'use strict';
(() => {
  const VERSION = 1;
  const ID = /^[0-9a-f]{32}$/;
  const HEIGHT_MIN = 480;
  const HEIGHT_MAX = 1600;
  const SNAPSHOT_KEYS = ['project', 'jobs', 'events', 'records', 'hostExecutions',
    'hostObservedAt', 'following', 'error'];
  const JOB_KEYS = ['job_id', 'project', 'state', 'preset', 'label', 'created_at', 'started_at',
    'finished_at', 'exit_code', 'action'];
  const EVENT_KEYS = ['project', 'call_id', 'tool', 'phase', 'time'];
  const RECORD_KEYS = ['schema_version', 'project', 'source_id', 'source_kind', 'actor_id',
    'actor_kind', 'actor_label', 'task_id', 'task_kind', 'task_label', 'action', 'state',
    'time', 'parent_task_id', 'parent_actor_id', 'parent_source_id', 'exit_code',
    'activity_role', 'search_scope', 'tool_name', 'model', 'reasoning_effort', 'model_source',
    'reasoning_effort_source', 'usage', 'usage_scope', 'usage_estimated', 'provenance',
    'received_at', 'expires_at', 'skill_id', 'skill_name'];
  const HOST_KEYS = ['project', 'operation_id', 'kind', 'state', 'created_at', 'started_at',
    'finished_at', 'result', 'termination_verified'];
  const RESULT_KEYS = ['exit_code', 'duration_ms', 'stdout_bytes', 'stderr_bytes'];
  const USAGE_KEYS = ['input_tokens', 'output_tokens', 'reasoning_tokens', 'total_tokens'];
  const HOST_STATES = new Set(['prepared', 'queued', 'running', 'succeeded', 'failed', 'cancelled',
    'outcome_unknown']);
  const TERMINAL_HOST_STATES = new Set(['succeeded', 'failed', 'cancelled', 'outcome_unknown']);
  const HOST_PHASES = new Set(['PREPARED', 'QUEUED', 'PRECHECK', 'EXECUTE', 'COMPLETE', 'RECOVER']);
  const WORK_ACTIONS = new Set(['read', 'search', 'reason', 'code', 'build', 'test',
    'review', 'execute', 'commit', 'push']);
  const SKILL_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$/;

  const plain = value => value && typeof value === 'object' && !Array.isArray(value)
    && Object.prototype.toString.call(value) === '[object Object]';
  function scalar(value) {
    if (value === null || typeof value === 'boolean') return value;
    if (typeof value === 'number' && Number.isSafeInteger(value)) return value;
    if (typeof value === 'string' && value.length <= 4096 && !value.includes('\0')) return value;
    return undefined;
  }
  function select(value, keys, nested = {}) {
    if (!plain(value)) return null;
    const result = {};
    for (const key of keys) {
      if (!Object.hasOwn(value, key)) continue;
      const selected = nested[key] ? select(value[key], nested[key]) : scalar(value[key]);
      if (selected !== undefined && selected !== null) result[key] = selected;
      else if (value[key] === null) result[key] = null;
    }
    return result;
  }
  function list(value, keys, nested) {
    if (!Array.isArray(value)) return [];
    return value.slice(0, 1000).map(item => select(item, keys, nested)).filter(Boolean);
  }
  const time = value => typeof value === 'string' && Number.isFinite(Date.parse(value));
  const boundedResult = value => plain(value) && RESULT_KEYS.every(key => Number.isSafeInteger(value[key])
    && (key === 'exit_code' ? value[key] >= -255 && value[key] <= 255
      : value[key] >= 0 && value[key] <= 10000000));
  const displayText = value => typeof value === 'string' && value.trim().length > 0
    && Array.from(value).length <= 160 && !/\p{C}/u.test(value);
  function workRecord(value) {
    const result = select(value, RECORD_KEYS, {usage: USAGE_KEYS});
    if (!result) return null;
    const hasSkillId = Object.hasOwn(value, 'skill_id');
    const hasSkillName = Object.hasOwn(value, 'skill_name');
    const skillId = value.skill_id;
    const skillName = value.skill_name;
    const validSkillId = typeof skillId === 'string' && SKILL_ID.test(skillId);
    const validSkillName = typeof skillName === 'string' && Array.from(skillName).length >= 1
      && Array.from(skillName).length <= 80 && !/\p{C}/u.test(skillName);
    if (value.task_kind !== 'call' && (hasSkillId || hasSkillName)
        || hasSkillId && !validSkillId || hasSkillName && (!validSkillId || !validSkillName)) {
      delete result.skill_id;
      delete result.skill_name;
    }
    return result;
  }
  function workContext(value) {
    if (!plain(value) || !displayText(value.command_label) || !WORK_ACTIONS.has(value.action)
        || !(value.label_source === 'reported' && displayText(value.task_label)
          || value.label_source === 'unavailable' && value.task_label === null)) return null;
    return select(value, ['task_label', 'label_source', 'command_label', 'action']);
  }
  function displayHost(value, project) {
    if (!plain(value) || value.project !== project || !ID.test(value.operation_id || '')
        || !['command', 'codex'].includes(value.kind) || !HOST_STATES.has(value.state)
        || ![null, true, false].includes(value.termination_verified) || !time(value.created_at)
        || (value.started_at !== null && !time(value.started_at))
        || (value.finished_at !== null && !time(value.finished_at))) return null;
    const authorization = value.authorization;
    if (!plain(authorization) || authorization.kind !== 'host_access'
        || !ID.test(authorization.grant_id || '') || !time(authorization.expires_at)) return null;
    const terminal = TERMINAL_HOST_STATES.has(value.state);
    if ((value.state === 'running' && value.started_at === null) || (terminal && value.finished_at === null)
        || (!terminal && value.finished_at !== null) || (value.result !== null && !boundedResult(value.result))
        || (value.state === 'succeeded' && (value.termination_verified !== true
          || !boundedResult(value.result) || value.result.exit_code !== 0))) return null;
    let metadata;
    if (value.kind === 'codex') {
      const input = value.agent_metadata;
      if (!plain(input) || typeof input.profile_id !== 'string' || input.profile_id.length > 120
          || typeof input.model !== 'string' || input.model.length > 120
          || typeof input.reasoning_effort !== 'string' || input.reasoning_effort.length > 32
          || !/^[a-f0-9]{64}$/.test(input.profile_digest || '')) return null;
      metadata = select(input, ['profile_id', 'model', 'reasoning_effort']);
    }
    const result = select(value, HOST_KEYS, {result: RESULT_KEYS});
    result.display_view = true;
    const context = workContext(value.work_context);
    if (context) result.work_context = context;
    if (HOST_PHASES.has(value.phase)) result.phase = value.phase;
    if (value.code === null || typeof value.code === 'string'
        && /^[A-Z][A-Z0-9_]{0,95}$/.test(value.code)) result.code = value.code;
    if (metadata) result.agent_metadata = metadata;
    return result;
  }
  function sanitize(input) {
    const project = scalar(input?.project) || '';
    const snapshot = {
      project,
      jobs: list(input?.jobs, JOB_KEYS).filter(row => row.project === project),
      events: list(input?.events, EVENT_KEYS).filter(row => row.project === project),
      records: (Array.isArray(input?.records) ? input.records : [])
        .slice(0, 1000).map(workRecord).filter(row => row && row.project === project),
      hostExecutions: (Array.isArray(input?.hostExecutions) ? input.hostExecutions : [])
        .slice(0, 1000).map(row => displayHost(row, project)).filter(Boolean),
      hostObservedAt: scalar(input?.hostObservedAt) ?? null,
      following: input?.following === true,
      error: scalar(input?.error) || '',
    };
    return Object.fromEntries(SNAPSHOT_KEYS.map(key => [key, snapshot[key]]));
  }
  const exact = (value, keys) => plain(value)
    && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
  const countSummary = value => {
    if (!exact(value, ['active', 'failed', 'attention', 'unknown', 'total'])) return null;
    if (!Object.values(value).every(item => Number.isSafeInteger(item) && item >= 0 && item <= 10000)) return null;
    return {...value};
  };

  function create(iframe, {onSummary = () => {}, onJobSelect = () => {},
    onHostExecutionSelect = () => {}} = {}) {
    if (!iframe || !iframe.contentWindow || !iframe.style) throw new TypeError('Workroom iframe required');
    const freshNonce = () => {
      if (typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
      const values = new Uint8Array(16);
      window.crypto.getRandomValues(values);
      return Array.from(values, value => value.toString(16).padStart(2, '0')).join('');
    };
    let epoch = 0;
    let nonce = freshNonce();
    let current = null;
    let destroyed = false;
    let loadedOnce = false;
    let restoring = false;
    let jobs = new Set();
    let operations = new Set();
    const send = message => {
      if (!destroyed) iframe.contentWindow.postMessage(message, '*');
    };
    const deliverCurrent = () => { if (current) send(current); };
    function receive(event) {
      if (destroyed || event.source !== iframe.contentWindow || event.origin !== 'null' || !plain(event.data)) return;
      const message = event.data;
      if (message.type === 'personal-ops:workroom:ready') {
        if (exact(message, ['type', 'version']) && message.version === VERSION) deliverCurrent();
        return;
      }
      if (message.version !== VERSION || message.epoch !== epoch || message.project !== current?.snapshot.project
          || message.nonce !== nonce) return;
      if (message.type === 'personal-ops:workroom:height'
          && exact(message, ['type', 'version', 'epoch', 'project', 'nonce', 'height'])
          && Number.isSafeInteger(message.height) && message.height >= HEIGHT_MIN && message.height <= HEIGHT_MAX) {
        iframe.style.height = `${message.height}px`;
      } else if (message.type === 'personal-ops:workroom:summary'
          && exact(message, ['type', 'version', 'epoch', 'project', 'nonce', 'counts'])) {
        const counts = countSummary(message.counts);
        if (counts) onSummary(counts);
      } else if (message.type === 'personal-ops:workroom:select-job'
          && exact(message, ['type', 'version', 'epoch', 'project', 'nonce', 'jobId'])
          && jobs.has(message.jobId)) {
        onJobSelect(message.jobId);
      } else if (message.type === 'personal-ops:workroom:select-host-execution'
          && exact(message, ['type', 'version', 'epoch', 'project', 'nonce', 'operationId'])
          && operations.has(message.operationId)) {
        onHostExecutionSelect(message.operationId);
      }
    }
    window.addEventListener('message', receive);
    function update(input) {
      const nextEpoch = input?.projectEpoch;
      if (!Number.isSafeInteger(nextEpoch) || nextEpoch < 0) return;
      epoch = nextEpoch;
      const snapshot = sanitize(input);
      jobs = new Set(snapshot.jobs.filter(row => row.project === snapshot.project && ID.test(row.job_id || ''))
        .map(row => row.job_id));
      operations = new Set(snapshot.hostExecutions
        .filter(row => row.project === snapshot.project && ID.test(row.operation_id || ''))
        .map(row => row.operation_id));
      current = {type: 'personal-ops:workroom:update', version: VERSION, epoch, nonce, snapshot};
      send(current);
    }
    function clear(project = '', projectEpoch = epoch + 1) {
      update({project, projectEpoch, jobs: [], events: [], records: [], hostExecutions: [],
        hostObservedAt: null, following: false, error: ''});
    }
    function setLive(value) {
      if (!current) return;
      current = {...current, snapshot: {...current.snapshot, following: value === true}};
      send(current);
    }
    function destroy() {
      destroyed = true; current = null; jobs.clear(); operations.clear();
      window.removeEventListener('message', receive);
      iframe.removeEventListener('load', loaded);
    }
    function loaded() {
      nonce = freshNonce();
      if (current) current = {...current, nonce};
      if (loadedOnce && !restoring) {
        restoring = true;
        iframe.setAttribute('src', '/workroom-frame.html');
        return;
      }
      loadedOnce = true;
      restoring = false;
      deliverCurrent();
    }
    iframe.addEventListener('load', loaded);
    return {update, clear, setLive, destroy};
  }
  window.WorkroomBridge = {create};
})();
