(function (root, factory) {
  'use strict';

  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.WorkDashboardModel = api;
}(typeof window === 'object' ? window : null, function () {
  'use strict';

  const CALL_ID = /^[A-Za-z0-9_.:-]{1,80}$/;
  const JOB_ID = /^[a-f0-9]{32}$/;
  const PHASES = new Set(['started', 'completed', 'failed', 'canceled']);
  const JOB_STATES = new Set([
    'queued', 'running', 'canceling', 'succeeded', 'failed', 'timed_out',
    'canceled', 'interrupted',
  ]);
  const HOST_STATES = new Set(['prepared', 'queued', 'running', 'succeeded', 'failed', 'cancelled', 'outcome_unknown']);
  const HOST_KINDS = new Set(['command', 'codex']);
  const HOST_PHASES = new Set(['PREPARED', 'QUEUED', 'PRECHECK', 'EXECUTE', 'COMPLETE', 'RECOVER']);
  const HOST_PHASE_LABELS = {PREPARED: '준비', QUEUED: '대기', PRECHECK: '실행 전 점검',
    EXECUTE: '명령 실행', COMPLETE: '실행 종료', RECOVER: '이전 실행 상태 확인'};
  const ACTIVE_STATES = new Set(['started', 'running', 'queued', 'canceling']);
  const ATTENTION_STATES = new Set(['failed', 'timed_out', 'canceled', 'interrupted']);
  const TERMINAL_STATES = new Set(['returned', 'succeeded', ...ATTENTION_STATES]);
  const ACTION_ROOMS = {read: 'analysis', search: 'analysis', reason: 'analysis', code: 'coding', build: 'testing',
    test: 'testing', review: 'review', execute: 'execution', commit: 'review', push: 'review'};
  const SOURCE_KINDS = new Set(['mcp', 'chatgpt', 'codex-cli', 'claude-cli']);
  const METADATA_ID = /^[A-Za-z0-9_.:+/-]+$/;
  const SKILL_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$/;
  const ACTIVITY_ROLES = new Set(['analysis', 'research', 'planning', 'debugging', 'review', 'summary']);
  const ROLE_ROOMS = {analysis: 'analysis', research: 'analysis', planning: 'analysis', debugging: 'analysis',
    review: 'review', summary: 'review'};
  const SEARCH_SCOPES = new Set(['web', 'files']);
  const USAGE_KEYS = ['input_tokens', 'output_tokens', 'reasoning_tokens', 'total_tokens'];
  const USAGE_SCOPES = new Set(['request', 'turn', 'session', 'task']);
  const ACTOR_KINDS = new Set(['mcp', 'executor', 'agent']);
  const TASK_KINDS = new Set(['call', 'job', 'agent']);
  const RECORD_STATES = new Set([...ACTIVE_STATES, ...TERMINAL_STATES, 'unknown']);
  const STATE_RANK = {queued: 0, started: 1, running: 2, canceling: 3,
    returned: 4, succeeded: 4, canceled: 5, interrupted: 6, timed_out: 7, failed: 8, unknown: 9};
  const LEGACY_SOURCE = 'personal-ops-tunnel';
  const TOOL_ROOMS = new Map([
    ['read_file', 'analysis'],
    ['list_dir', 'analysis'],
    ['find_file', 'analysis'],
    ['search_for_pattern', 'analysis'],
    ['create_text_file', 'coding'],
    ['replace_content', 'coding'],
    ['prepare_git_operation', 'review'],
    ['execute_git_operation', 'review'],
    ['get_git_operation', 'review'],
    ['git_status', 'review'],
    ['git_diff', 'review'],
    ['start_command', 'execution'],
    ['get_job', 'analysis'],
    ['read_job_log', 'analysis'],
    ['cancel_job', 'execution'],
    ['list_jobs', 'analysis'],
    ['read_project_memory', 'analysis'],
    ['update_project_memory', 'review'],
    ['read_project_progress', 'analysis'],
    ['update_project_progress', 'review'],
    ['list_execution_profiles', 'analysis'],
    ['get_runtime_status', 'analysis'],
    ['get_db_migration_status', 'analysis'],
    ['get_predecessor_migration_status', 'analysis'],
    ['get_schema014_migration_status', 'analysis'],
    ['get_host_operation', 'analysis'],
    ['compare_deployment', 'review'],
    ['prepare_test_deploy', 'review'],
    ['prepare_db_migration', 'review'],
    ['prepare_predecessor_migration', 'review'],
    ['prepare_schema014_migration', 'review'],
    ['execute_test_deploy', 'execution'],
    ['execute_db_migration', 'execution'],
    ['execute_predecessor_migration', 'execution'],
    ['execute_schema014_migration', 'execution'],
    ['request_host_access', 'review'],
    ['get_host_access', 'analysis'],
    ['start_host_execution', 'execution'],
    ['get_host_execution', 'analysis'],
    ['read_host_execution_log', 'analysis'],
    ['cancel_host_execution', 'execution'],
    ['list_host_command_profiles', 'analysis'],
    ['prepare_host_command', 'review'],
    ['execute_host_command', 'execution'],
    ['get_host_command', 'analysis'],
    ['cancel_host_command', 'execution'],
    ['list_agent_profiles', 'analysis'],
    ['prepare_agent_task', 'review'],
    ['start_agent_task', 'execution'],
    ['get_agent_task', 'analysis'],
    ['cancel_agent_task', 'execution'],
    ['list_host_targets', 'analysis'],
    ['inspect_host_target', 'analysis'],
    ['prepare_host_operation', 'review'],
    ['execute_host_operation', 'execution'],
    ['read_host_operation_events', 'analysis'],
    ['prepare_runtime_rollout', 'review'],
    ['get_runtime_rollout', 'analysis'],
    ['execute_runtime_rollout', 'execution'],
    ['open_host_access_approval', 'review'],
    ['update_host_access_approval', 'review'],
    ['confirm_host_access_approval', 'review'],
    ['cancel_host_access_approval', 'review'],
    ['open_schema014_approval', 'review'],
    ['confirm_schema014_approval', 'review'],
    ['cancel_schema014_approval', 'review'],
  ]);

  function sourceRoomForTool(tool) {
    if (typeof tool !== 'string') return null;
    return TOOL_ROOMS.get(tool) || null;
  }

  function timestamp(value) {
    if (typeof value !== 'string' || value.length === 0) return null;
    const milliseconds = Date.parse(value);
    if (!Number.isFinite(milliseconds)) return null;
    return {milliseconds, iso: new Date(milliseconds).toISOString()};
  }

  function numericResult(exitCode) {
    return Number.isSafeInteger(exitCode) && exitCode >= -255 && exitCode <= 255
      ? {exit_code: exitCode} : null;
  }

  function identity(sourceId, sourceKind, actorId, kind, label) {
    return {id: `entity:${encodeURIComponent(sourceId)}:${encodeURIComponent(actorId)}`,
      sourceId, sourceKind, localId: actorId, kind, label};
  }

  function actionForTool(tool, room) {
    if (tool === 'search_for_pattern' || tool === 'find_file') return 'search';
    if (tool === 'git_commit') return 'commit';
    if (tool === 'git_push') return 'push';
    return {analysis: 'read', coding: 'code', review: 'review', execution: 'execute'}[room];
  }

  function actionForJob(label) {
    if (/(^|[\s:/._-])(test|tests|typecheck|type-check|lint|e2e|check)([\s:/._-]|$)/i.test(label)) return 'test';
    return /(^|[\s:/._-])build([\s:/._-]|$)/i.test(label) ? 'build' : 'execute';
  }

  function normalizeEvents(project, events, now) {
    const selected = new Map();
    const starts = new Map();
    const phaseRank = {started: 0, completed: 1, canceled: 2, failed: 3};

    for (const candidate of Array.isArray(events) ? events : []) {
      if (!candidate || candidate.project !== project || typeof candidate.call_id !== 'string'
          || !CALL_ID.test(candidate.call_id)
          || !PHASES.has(candidate.phase)) continue;
      const sourceRoom = sourceRoomForTool(candidate.tool);
      const parsed = timestamp(candidate.time);
      if (!sourceRoom || !parsed) continue;
      if (candidate.phase === 'started') {
        const key = candidate.call_id + '/' + candidate.tool;
        const previousStart = starts.get(key);
        if (!previousStart || parsed.milliseconds < previousStart.milliseconds) starts.set(key, parsed);
      }

      const previous = selected.get(candidate.call_id);
      if (!previous || parsed.milliseconds > previous.parsed.milliseconds
          || (parsed.milliseconds === previous.parsed.milliseconds
              && phaseRank[candidate.phase] > phaseRank[previous.event.phase])) {
        selected.set(candidate.call_id, {event: candidate, parsed, sourceRoom});
      }
    }

    return Array.from(selected.values(), ({event, parsed, sourceRoom}) => {
      let room = sourceRoom;
      let state = event.phase;
      if (event.phase === 'completed') {
        room = 'done';
        state = 'returned';
      } else if (event.phase === 'failed' || event.phase === 'canceled') {
        room = 'attention';
      } else if (now - parsed.milliseconds > 120000) {
        room = 'waiting';
        state = 'unknown';
      }
      const result = numericResult(event.exit_code);
      return {
        id: `call:${event.call_id}`,
        kind: 'call',
        room,
        sourceRoom,
        state,
        label: event.tool,
        time: parsed.iso,
        jobId: null,
        tool: event.tool,
        action: actionForTool(event.tool, sourceRoom),
        actorId: `entity:${LEGACY_SOURCE}:coding-mcp`,
        provenance: 'observed',
        replayable: TERMINAL_STATES.has(state) && Boolean(starts.get(event.call_id + '/' + event.tool))
          && starts.get(event.call_id + '/' + event.tool).milliseconds <= parsed.milliseconds,
        _invokedAt: starts.get(event.call_id + '/' + event.tool)?.iso
          || (TERMINAL_STATES.has(state) || state === 'unknown' ? parsed.iso : null),
        _entity: identity(LEGACY_SOURCE, 'mcp', 'coding-mcp', 'mcp', '코딩 MCP'),
        _milliseconds: parsed.milliseconds,
        ...(result ? {result} : {}),
      };
    });
  }

  function validJobTimes(candidate) {
    const created = timestamp(candidate.created_at);
    if (!created) return null;
    const started = candidate.started_at == null ? null : timestamp(candidate.started_at);
    const finished = candidate.finished_at == null ? null : timestamp(candidate.finished_at);
    if ((candidate.started_at != null && !started) || (candidate.finished_at != null && !finished)) {
      return null;
    }
    return {created, started, finished, display: finished || started || created};
  }

  function normalizeJobs(project, jobs, now) {
    const actors = [];
    for (const candidate of Array.isArray(jobs) ? jobs : []) {
      if (!candidate || candidate.project !== project || typeof candidate.job_id !== 'string' || !JOB_ID.test(candidate.job_id)
          || !JOB_STATES.has(candidate.state)) continue;
      const times = validJobTimes(candidate);
      if (!times) continue;

      const rawLabel = (typeof candidate.preset === 'string' && candidate.preset)
        || (typeof candidate.label === 'string' && candidate.label)
        || '명령 실행';
      const label = rawLabel.slice(0, 200);
      const action = typeof candidate.action === 'string' && Object.hasOwn(ACTION_ROOMS, candidate.action)
        ? candidate.action : actionForJob(label);
      const sourceRoom = ACTION_ROOMS[action];
      let room = sourceRoom;
      let state = candidate.state;
      if (candidate.state === 'queued') room = 'waiting';
      else if (candidate.state === 'succeeded') {
        if (candidate.exit_code === 0) room = 'done';
        else {
          room = 'waiting';
          state = 'unknown';
        }
      } else if (ATTENTION_STATES.has(candidate.state)) room = 'attention';

      const result = numericResult(candidate.exit_code);
      actors.push({
        id: `job:${candidate.job_id}`,
        kind: 'job',
        room,
        sourceRoom,
        state,
        label,
        time: times.display.iso,
        jobId: candidate.job_id,
        tool: null,
        action,
        actorId: `entity:${LEGACY_SOURCE}:commands`,
        provenance: 'observed',
        replayable: TERMINAL_STATES.has(state) && Boolean(times.started && times.finished)
          && times.started.milliseconds <= times.finished.milliseconds,
        _invokedAt: candidate.state === 'queued' ? null
          : times.started?.iso || (['running', 'canceling'].includes(candidate.state)
            ? new Date(now).toISOString()
            : TERMINAL_STATES.has(state) || state === 'unknown' ? times.display.iso : null),
        _entity: identity(LEGACY_SOURCE, 'mcp', 'commands', 'executor', '명령 실행기'),
        _milliseconds: times.display.milliseconds,
        ...(result ? {result} : {}),
      });
    }
    return actors;
  }

  function metadataRow(row, parsed, received, order) {
    const result = {time: parsed.iso, milliseconds: parsed.milliseconds,
      receivedMilliseconds: received?.milliseconds ?? parsed.milliseconds, order,
      provenance: row.provenance === 'observed' ? 'observed' : 'reported'};
    if (typeof row.model === 'string' && row.model.length <= 120 && METADATA_ID.test(row.model)) result.model = row.model;
    if (result.model && ['configuration', 'event'].includes(row.model_source)) result.modelSource = row.model_source;
    if (typeof row.reasoning_effort === 'string' && row.reasoning_effort.length <= 32
        && METADATA_ID.test(row.reasoning_effort)) result.reasoningEffort = row.reasoning_effort;
    const effortSource = row.reasoning_effort_source ?? result.modelSource;
    if (result.reasoningEffort && ['configuration', 'event'].includes(effortSource)) result.reasoningEffortSource = effortSource;
    if (row.usage && typeof row.usage === 'object' && !Array.isArray(row.usage) && USAGE_SCOPES.has(row.usage_scope)) {
      const values = Object.fromEntries(USAGE_KEYS.filter(key => Object.hasOwn(row.usage, key)).map(key => [key, row.usage[key]]));
      if (Object.keys(values).length && Object.values(values).every(value => Number.isSafeInteger(value) && value >= 0)) {
        result.usage = values; result.usageScope = row.usage_scope;
        if (typeof row.usage_estimated === 'boolean') result.usageEstimated = row.usage_estimated;
      }
    }
    const skillMatch = typeof row.skill_id === 'string' ? SKILL_ID.exec(row.skill_id) : null;
    if (row.task_kind === 'call' && skillMatch?.[0] === row.skill_id && !/\p{C}/u.test(row.skill_id)) {
      result.skillId = row.skill_id;
      if (typeof row.skill_name === 'string' && Array.from(row.skill_name).length >= 1
          && Array.from(row.skill_name).length <= 80 && !/\p{C}/u.test(row.skill_name)) {
        result.skillName = row.skill_name;
      }
    }
    return result;
  }

  function skillFor(rows) {
    const withSkill = rows.filter(row => row.skillId);
    const ids = new Set(withSkill.map(row => row.skillId));
    if (ids.size !== 1) return null;
    const id = withSkill[0].skillId;
    const named = [...withSkill].sort((a, b) => a.milliseconds - b.milliseconds
      || a.receivedMilliseconds - b.receivedMilliseconds || b.order - a.order)
      .filter(row => row.skillId === id && row.skillName).at(-1);
    const provenance = withSkill.some(row => row.provenance === 'reported') ? 'reported' : 'observed';
    return {id, ...(named ? {name: named.skillName} : {}), provenance};
  }

  function telemetryFor(rows) {
    let result = {};
    // API records are newest-first; equal stamps retain the persisted row order.
    for (const row of [...rows].sort((a, b) => a.milliseconds - b.milliseconds
        || a.receivedMilliseconds - b.receivedMilliseconds || b.order - a.order)) {
      // Metadata from one model must never be attributed to a later, different model.
      if (row.model && row.model !== result.model?.value) result = {};
      for (const key of ['model', 'reasoningEffort', 'usage']) if (Object.hasOwn(row, key)) {
        const source = key === 'model' ? row.modelSource : row.reasoningEffortSource;
        result[key] = {value: row[key], time: row.time, provenance: row.provenance,
          ...(key === 'usage' ? {scope: row.usageScope,
            ...(row.usageEstimated !== undefined ? {estimated: row.usageEstimated} : {})}
            : source ? {source} : {})};
      }
    }
    return result;
  }

  function claudeLifecycleFor(row) {
    if (row.source_kind !== 'claude-cli' || row.actor_kind !== 'agent' || row.provenance !== 'observed'
        || row.task_kind !== 'call' || row.action !== 'execute') return null;
    if (row.task_id.startsWith('prompt:') && row.task_id.length > 7) return 'prompt';
    return row.actor_id.startsWith('sub:') && row.task_id === `agent:${row.actor_id.slice(4)}` ? 'subagent' : null;
  }

  // Adapters provide explicit identities; CLI/model/agent identities are never inferred from tool names.
  function normalizeRecords(project, records, now) {
    const selected = new Map();
    const starts = new Map();
    const lifecycleStarts = new Map();
    const metadata = new Map();
    for (const [order, row] of (Array.isArray(records) ? records : []).entries()) {
      if (!row || row.schema_version !== 1 || row.project !== project
          || !SOURCE_KINDS.has(row.source_kind) || !ACTOR_KINDS.has(row.actor_kind)
          || !TASK_KINDS.has(row.task_kind) || !RECORD_STATES.has(row.state)
          || (row.task_kind === 'call' && row.state === 'succeeded')
          || (row.task_kind !== 'call' && row.state === 'returned')
          || typeof row.action !== 'string' || !Object.hasOwn(ACTION_ROOMS, row.action)
          || ![row.source_id, row.actor_id, row.task_id].every(value => typeof value === 'string' && CALL_ID.test(value))) continue;
      const parsed = timestamp(row.time);
      const expiry = row.expires_at === undefined ? null : timestamp(row.expires_at);
      const received = row.received_at === undefined ? null : timestamp(row.received_at);
      if (!parsed || (row.expires_at !== undefined && !expiry)
          || (row.received_at !== undefined && !received)) continue;
      const actor = identity(row.source_id, row.source_kind, row.actor_id, row.actor_kind,
        typeof row.actor_label === 'string' && row.actor_label ? row.actor_label.slice(0, 80) : row.actor_kind);
      const namespace = `${encodeURIComponent(row.source_id)}:${encodeURIComponent(row.actor_id)}`;
      const id = `task:${namespace}:${encodeURIComponent(row.task_id)}`;
      if (!metadata.has(id)) metadata.set(id, []);
      metadata.get(id).push(metadataRow(row, parsed, received, order));
      if (row.state === 'started' || row.state === 'running') {
        const previousStart = starts.get(id);
        if (!previousStart || parsed.milliseconds < previousStart) starts.set(id, parsed.milliseconds);
        if (claudeLifecycleFor(row)) {
          const previous = lifecycleStarts.get(id);
          if (previous === undefined || parsed.milliseconds < previous) lifecycleStarts.set(id, parsed.milliseconds);
        }
      }
      const previous = selected.get(id);
      if (previous && (previous.parsed.milliseconds > parsed.milliseconds
          || (previous.parsed.milliseconds === parsed.milliseconds
            && STATE_RANK[previous.row.state] >= STATE_RANK[row.state]))) continue;
      selected.set(id, {row, actor, parsed, expiry, received});
    }
    return Array.from(selected, ([id, {row, actor, parsed, expiry, received}]) => {
      let state = row.state;
      if (row.task_kind === 'job' && state === 'succeeded' && row.exit_code !== 0) state = 'unknown';
      const deadline = Math.min(parsed.milliseconds, received?.milliseconds ?? parsed.milliseconds) + 120000;
      const observationExpired = ACTIVE_STATES.has(state) && now >= Math.min(expiry?.milliseconds ?? deadline, deadline);
      if (observationExpired) state = 'unknown';
      const sourceRoom = ACTION_ROOMS[row.action];
      const room = state === 'returned' || state === 'succeeded' ? 'done'
        : ATTENTION_STATES.has(state) ? 'attention'
          : state === 'queued' || state === 'unknown' ? 'waiting' : sourceRoom;
      const activityRole = typeof row.activity_role === 'string' && ACTIVITY_ROLES.has(row.activity_role)
        ? row.activity_role : null;
      const searchScope = row.action === 'search' && typeof row.search_scope === 'string'
        && SEARCH_SCOPES.has(row.search_scope) ? row.search_scope : null;
      const tool = typeof row.tool_name === 'string' && row.tool_name.length <= 120
        && METADATA_ID.test(row.tool_name) ? row.tool_name : null;
      const result = numericResult(row.exit_code);
      const taskLabelKnown = typeof row.task_label === 'string' && Boolean(row.task_label);
      const skill = row.task_kind === 'call' ? skillFor(metadata.get(id)) : null;
      const sessionActivity = row.source_kind === 'codex-cli' && row.task_kind === 'agent'
        && row.task_id === 'session-activity';
      return {id, kind: row.task_kind, actorId: actor.id, label: typeof row.task_label === 'string'
        && row.task_label ? row.task_label.slice(0, 200) : row.action, action: row.action,
        state, room, sourceRoom, time: parsed.iso, tool, activityRole, searchScope, jobId: null,
        provenance: row.provenance === 'observed' ? 'observed' : 'reported',
        telemetry: telemetryFor(metadata.get(id)),
        replayable: TERMINAL_STATES.has(state) && starts.has(id) && starts.get(id) <= parsed.milliseconds,
        _invokedAt: starts.has(id) ? new Date(starts.get(id)).toISOString()
          : row.source_kind === 'codex-cli' && row.task_kind === 'call' && state === 'queued' ? parsed.iso
          : TERMINAL_STATES.has(state) || state === 'unknown' ? parsed.iso : null,
        parentTaskId: typeof row.parent_task_id === 'string' && CALL_ID.test(row.parent_task_id)
          ? `task:${encodeURIComponent(typeof row.parent_source_id === 'string' && CALL_ID.test(row.parent_source_id)
            ? row.parent_source_id : row.source_id)}:${encodeURIComponent(
            typeof row.parent_actor_id === 'string' && CALL_ID.test(row.parent_actor_id) ? row.parent_actor_id : row.actor_id
          )}:${encodeURIComponent(row.parent_task_id)}` : null,
        _entity: actor, _milliseconds: parsed.milliseconds,
        _observationMilliseconds: received?.milliseconds ?? parsed.milliseconds,
        _observationExpired: observationExpired, _claudeLifecycle: claudeLifecycleFor(row),
        _claudeStartedAt: lifecycleStarts.get(id),
        _telemetryRows: metadata.get(id), _taskLabelKnown: taskLabelKnown,
        ...(sessionActivity ? {sessionActivity: true} : {}),
        ...(claudeLifecycleFor(row) === 'subagent' ? {agentActivity: true} : {}),
        ...(skill ? {skill} : {}),
        ...(result ? {result} : {})};
    });
  }

  function validHostResult(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
    return ['exit_code', 'duration_ms', 'stdout_bytes', 'stderr_bytes'].every(key =>
      Number.isSafeInteger(value[key]) && (key === 'exit_code'
        ? value[key] >= -255 && value[key] <= 255
        : value[key] >= 0 && value[key] <= 10000000));
  }

  function publicHostResult(value) {
    return Object.fromEntries(['exit_code', 'duration_ms', 'stdout_bytes', 'stderr_bytes']
      .map(key => [key, value[key]]));
  }

  function validAgentMetadata(value) {
    return value && typeof value === 'object' && !Array.isArray(value)
      && typeof value.profile_id === 'string' && CALL_ID.test(value.profile_id)
      && typeof value.model === 'string' && value.model.length <= 120 && METADATA_ID.test(value.model)
      && typeof value.reasoning_effort === 'string' && value.reasoning_effort.length <= 32
      && METADATA_ID.test(value.reasoning_effort)
      && typeof value.profile_digest === 'string' && /^[a-f0-9]{64}$/.test(value.profile_digest);
  }

  function validDisplayAgentMetadata(value) {
    return value && typeof value === 'object' && !Array.isArray(value)
      && Object.keys(value).length === 3
      && typeof value.profile_id === 'string' && CALL_ID.test(value.profile_id)
      && typeof value.model === 'string' && value.model.length <= 120 && METADATA_ID.test(value.model)
      && typeof value.reasoning_effort === 'string' && value.reasoning_effort.length <= 32
      && METADATA_ID.test(value.reasoning_effort);
  }

  function hostPhaseLabel(value) {
    return HOST_PHASE_LABELS[value] || '단계 미수집';
  }

  function boundedDisplayText(value) {
    if (typeof value !== 'string') return null;
    const normalized = value.trim();
    return normalized && Array.from(normalized).length <= 160 && !/\p{C}/u.test(normalized)
      ? normalized : null;
  }

  function publicWorkContext(value) {
    const unavailable = {taskLabel: null, labelSource: 'unavailable', commandLabel: null};
    const taskLabel = value?.task_label == null ? null : boundedDisplayText(value.task_label);
    const commandLabel = value?.command_label == null ? null : boundedDisplayText(value.command_label);
    if (!value || typeof value !== 'object' || Array.isArray(value)
        || !Object.keys(value).every(key => ['task_label', 'label_source', 'command_label', 'action'].includes(key))
        || !['reported', 'unavailable'].includes(value.label_source)
        || (value.task_label != null && taskLabel == null)
        || (value.command_label != null && commandLabel == null)
        || !(value.action == null || Object.hasOwn(ACTION_ROOMS, value.action))
        || (value.label_source === 'reported' && taskLabel == null)
        || (value.label_source === 'unavailable' && value.task_label != null)) return {context: unavailable, action: null};
    return {context: {taskLabel, labelSource: value.label_source, commandLabel}, action: value.action || null};
  }

  function validDisplayHost(value) {
    const allowed = new Set(['project', 'operation_id', 'kind', 'state', 'created_at', 'started_at',
      'finished_at', 'result', 'termination_verified', 'display_view', 'agent_metadata', 'phase', 'code',
      'work_context']);
    return value?.display_view === true && Object.keys(value).every(key => allowed.has(key))
      && !Object.hasOwn(value, 'authorization');
  }

  function normalizeHostExecutions(project, hostExecutions, hostObservedAt, now) {
    const observed = timestamp(hostObservedAt);
    const fresh = Boolean(observed && observed.milliseconds <= now + 5000
      && now - observed.milliseconds < 120000);
    const tasks = [];
    for (const candidate of Array.isArray(hostExecutions) ? hostExecutions : []) {
      const displayView = validDisplayHost(candidate);
      if (!candidate || candidate.project !== project || typeof candidate.operation_id !== 'string'
          || !JOB_ID.test(candidate.operation_id) || !HOST_KINDS.has(candidate.kind)
          || !HOST_STATES.has(candidate.state)
          || ![null, true, false].includes(candidate.termination_verified)) continue;
      const created = timestamp(candidate.created_at);
      const started = candidate.started_at == null ? null : timestamp(candidate.started_at);
      const finished = candidate.finished_at == null ? null : timestamp(candidate.finished_at);
      const authorization = candidate.authorization;
      if (!created || (candidate.started_at != null && !started) || (candidate.finished_at != null && !finished)
          || started && started.milliseconds < created.milliseconds
          || finished && finished.milliseconds < (started || created).milliseconds
          || (!displayView && (!authorization || authorization.kind !== 'host_access'
            || typeof authorization.grant_id !== 'string' || !JOB_ID.test(authorization.grant_id)
            || !timestamp(authorization.expires_at)))) continue;
      const terminal = ['succeeded', 'failed', 'cancelled', 'outcome_unknown'].includes(candidate.state);
      if ((candidate.state === 'running' && !started) || (terminal && !finished)
          || (!terminal && finished) || (candidate.result != null && !validHostResult(candidate.result))
          || (candidate.state === 'succeeded' && (candidate.termination_verified !== true
            || !validHostResult(candidate.result) || candidate.result.exit_code !== 0))) continue;
      const metadata = candidate.kind === 'codex' ? candidate.agent_metadata : null;
      if (candidate.kind === 'codex'
          && !(displayView ? validDisplayAgentMetadata(metadata) : validAgentMetadata(metadata))) continue;

      const work = publicWorkContext(candidate.work_context);
      const hostPhase = typeof candidate.phase === 'string' && HOST_PHASES.has(candidate.phase)
        ? candidate.phase : null;
      const hostCode = candidate.code == null ? null
        : typeof candidate.code === 'string' && /^[A-Z][A-Z0-9_]{0,95}$/.test(candidate.code)
          ? candidate.code : null;

      const transition = finished || started || created;
      const sourceId = candidate.kind === 'codex' ? 'personal-ops-agent' : 'personal-ops-host';
      const sourceKind = candidate.kind === 'codex' ? 'codex-cli' : 'mcp';
      const actorId = candidate.kind === 'codex' ? metadata.profile_id : 'host-executor';
      const actorKind = candidate.kind === 'codex' ? 'agent' : 'executor';
      const taskKind = candidate.kind === 'codex' ? 'agent' : 'job';
      const actor = identity(sourceId, sourceKind, actorId, actorKind,
        candidate.kind === 'codex' ? 'Codex host agent' : '호스트 실행기');
      const id = `task:${encodeURIComponent(sourceId)}:${encodeURIComponent(actorId)}:${candidate.operation_id}`;
      const mapped = {prepared: 'prepared', queued: 'queued', running: 'running', succeeded: 'succeeded',
        failed: 'failed', cancelled: 'canceled', outcome_unknown: 'unknown'}[candidate.state];
      const state = terminal || fresh ? mapped : 'unknown';
      const sourceRoom = candidate.kind === 'codex' ? 'coding' : 'execution';
      const room = state === 'succeeded' ? 'done' : ATTENTION_STATES.has(state) ? 'attention'
        : state === 'prepared' || state === 'queued' || state === 'unknown' ? 'waiting' : sourceRoom;
      const rows = candidate.kind === 'codex' ? [{time: transition.iso, milliseconds: transition.milliseconds,
        receivedMilliseconds: observed?.milliseconds ?? transition.milliseconds, order: -1,
        provenance: 'observed', model: metadata.model, modelSource: 'configuration',
        reasoningEffort: metadata.reasoning_effort, reasoningEffortSource: 'configuration'}] : [];
      const defaultAction = candidate.kind === 'codex' ? 'code' : 'execute';
      const action = work.action || defaultAction;
      // The host envelope classifies a Codex launch, not its inner tool activity.
      // Only a detailed agent record can make that internal action observable.
      const actionKnown = candidate.kind !== 'codex' && Boolean(work.action);
      const label = work.context.labelSource === 'reported'
        ? work.context.taskLabel : `${project} 작업 · 목적 미수집`;
      const actionRoom = ACTION_ROOMS[action];
      tasks.push({id, kind: taskKind, actorId: actor.id, label,
        action, state, room: state === 'running' && actionKnown ? actionRoom : room,
        sourceRoom: actionKnown ? actionRoom : sourceRoom,
        time: transition.iso, tool: null, jobId: null, hostExecutionId: candidate.operation_id,
        hostActionKnown: actionKnown,
        hostPhase, hostCode, observedAt: observed?.iso || null,
        workContext: work.context, commandLabel: work.context.commandLabel,
        provenance: 'observed', telemetry: telemetryFor(rows),
        replayable: terminal && Boolean(started && finished), parentTaskId: null,
        _invokedAt: candidate.state === 'prepared' || candidate.state === 'queued'
          ? null : started?.iso || (terminal || state === 'unknown' ? transition.iso : null),
        _entity: actor, _milliseconds: transition.milliseconds,
        _observationMilliseconds: observed?.milliseconds ?? -Infinity, _telemetryRows: rows,
        _hostAuthoritative: terminal || fresh, _hostContextActionKnown: actionKnown,
        ...(candidate.result != null ? {result: publicHostResult(candidate.result)} : {})});
    }
    return tasks;
  }

  function mergeHostTasks(tasks, hostTasks) {
    const merged = new Map(tasks.map(task => [task.id, task]));
    for (const host of hostTasks) {
      const existing = merged.get(host.id);
      if (!existing) {
        merged.set(host.id, host);
        continue;
      }
      const hostIsNewer = host._observationMilliseconds >= (existing._observationMilliseconds ?? existing._milliseconds);
      const stateSource = host._hostAuthoritative && hostIsNewer ? host : existing;
      const existingActionEvidence = existing.provenance === 'observed'
        && Boolean(existing.tool || existing.activityRole || existing.searchScope || existing.action !== host.action);
      const existingActionKnown = existingActionEvidence && existing.state !== 'unknown'
        && Object.hasOwn(ACTION_ROOMS, existing.action)
        && (stateSource === existing || ['started', 'running', 'canceling'].includes(existing.state));
      const hostActionKnown = host._hostContextActionKnown || existingActionKnown;
      const actionSource = host._hostContextActionKnown && !existingActionKnown ? host : existing;
      const resolvedAction = hostActionKnown ? actionSource.action : stateSource.action;
      const sourceRoom = hostActionKnown ? actionSource.sourceRoom : stateSource.sourceRoom;
      const room = hostActionKnown && stateSource === host && ['started', 'running', 'canceling'].includes(host.state)
        ? sourceRoom : stateSource.room;
      const telemetryRows = [...(existing._telemetryRows || []), ...(host._telemetryRows || [])];
      const invocationCandidates = [existing._invokedAt, host._invokedAt]
        .map(value => timestamp(value)).filter(Boolean).sort((left, right) => left.milliseconds - right.milliseconds);
      const matchedTaskLabel = host.workContext?.labelSource === 'unavailable'
        && existing._taskLabelKnown && ['observed', 'reported'].includes(existing.provenance)
        ? boundedDisplayText(existing.label) : null;
      const workContext = matchedTaskLabel ? {taskLabel: matchedTaskLabel,
        labelSource: existing.provenance, commandLabel: host.commandLabel} : host.workContext;
      merged.set(host.id, {...existing, ...stateSource,
        label: host.workContext?.labelSource === 'reported' ? host.label
          : existing.workContext?.labelSource === 'reported' ? existing.label
            : existing.label && !/^Host Codex task [A-Za-z0-9_-]+$/.test(existing.label) ? existing.label : host.label,
        action: resolvedAction, room, sourceRoom, hostActionKnown,
        tool: hostActionKnown ? actionSource.tool : stateSource.tool,
        activityRole: hostActionKnown ? actionSource.activityRole : stateSource.activityRole,
        searchScope: hostActionKnown ? actionSource.searchScope : stateSource.searchScope,
        workContext, commandLabel: host.commandLabel,
        hostPhase: host.hostPhase, hostCode: host.hostCode, observedAt: host.observedAt,
        parentTaskId: existing.parentTaskId, hostExecutionId: host.hostExecutionId,
        telemetry: telemetryFor(telemetryRows), _telemetryRows: telemetryRows,
        replayable: existing.replayable || host.replayable,
        _invokedAt: invocationCandidates[0]?.iso || null});
    }
    return [...merged.values()];
  }

  function actorPriority(actor) {
    if (actor.state === 'running' || actor.state === 'started' || actor.state === 'canceling') return 0;
    if (actor.state === 'queued') return 1;
    if (actor.state === 'unknown') return 2;
    return 3;
  }

  function snapshot({project, jobs = [], events = [], records = [], hostExecutions = [], hostObservedAt = null,
    objects = [], now = Date.now()} = {}) {
    const nowMilliseconds = typeof now === 'number' && Number.isFinite(now)
      ? now : Date.parse(now);
    const time = Number.isFinite(nowMilliseconds) ? nowMilliseconds : Date.now();
    const tasks = mergeHostTasks(normalizeEvents(project, events,
      time).concat(normalizeJobs(project, jobs, time), normalizeRecords(project, records, time)),
    normalizeHostExecutions(project, hostExecutions, hostObservedAt, time));
    const counts = {active: 0, done: 0, attention: 0, unknown: 0, total: tasks.length};
    for (const actor of tasks) {
      if (ACTIVE_STATES.has(actor.state)) counts.active += 1;
      else if (actor.state === 'unknown') counts.unknown += 1;
      else if (actor.state === 'returned' || actor.state === 'succeeded') counts.done += 1;
      else if (ATTENTION_STATES.has(actor.state)) counts.attention += 1;
    }

    tasks.sort((left, right) => actorPriority(left) - actorPriority(right)
      || Number(left.kind === 'agent') - Number(right.kind === 'agent')
      || right._milliseconds - left._milliseconds || left.id.localeCompare(right.id));
    const groups = new Map();
    for (const task of tasks) {
      if (!groups.has(task.actorId)) groups.set(task.actorId, []);
      groups.get(task.actorId).push(task);
    }
    const known = new Map();
    for (const object of Array.isArray(objects) ? objects : []) {
      if (!object || object.project !== project || !SOURCE_KINDS.has(object.sourceKind) || !ACTOR_KINDS.has(object.kind)
          || ![object.sourceId, object.localId].every(value => typeof value === 'string' && CALL_ID.test(value))) continue;
      const label = typeof object.baseLabel === 'string' ? object.baseLabel : object.label;
      const item = identity(object.sourceId, object.sourceKind, object.localId, object.kind,
        typeof label === 'string' && label ? label.slice(0, 80) : object.kind);
      if (item.id !== object.id) continue;
      known.set(item.id, item);
      if (!groups.has(item.id)) groups.set(item.id, []);
    }
    const actors = Array.from(groups, ([id, work]) => {
      if (!work.length) return {...known.get(id), project, state: 'unobserved', room: 'waiting', sourceRoom: 'waiting',
        action: 'idle', taskId: null, lastTaskId: null, time: null, jobId: null, activeCount: 0, taskCount: 0, telemetry: {}};
      const latest = work.reduce((a, b) => a._milliseconds >= b._milliseconds ? a : b);
      const nativeCall = work.some(task => task.sessionActivity || task.agentActivity)
        ? work.find(task => task.kind === 'call' && !task.agentActivity && ACTIVE_STATES.has(task.state)) : null;
      const pending = nativeCall || work.find(task => ACTIVE_STATES.has(task.state) || task.state === 'unknown');
      const activeWork = work.filter(task => ACTIVE_STATES.has(task.state));
      const current = pending || latest;
      // Lifecycle freshness is separate from physical idle/replay posture. A tool return
      // cannot close a request, nor can an unmatched start prove continued execution.
      const lifecycle = work.filter(task => task._claudeLifecycle && Number.isFinite(task._claudeStartedAt)).sort((left, right) =>
        right._claudeStartedAt - left._claudeStartedAt
        || Number(right._observationExpired) - Number(left._observationExpired)
        || left.id.localeCompare(right.id))[0];
      const observationStatus = lifecycle?._observationExpired ? {kind: lifecycle._claudeLifecycle,
        taskId: lifecycle.id, time: lifecycle.time} : null;
      return {...latest._entity, project, id, state: pending ? pending.state : 'idle',
        room: pending ? pending.room : 'waiting', sourceRoom: current.sourceRoom,
        action: pending ? pending.action : 'idle', taskId: pending ? pending.id : null,
        lastTaskId: latest.id, time: latest.time, jobId: pending ? pending.jobId : null,
        provenance: current.provenance,
        telemetry: telemetryFor(work.flatMap(task => task._telemetryRows || [])),
        ...(observationStatus ? {observationStatus} : {}),
        activeCount: activeWork.filter(task => !(nativeCall && (task.sessionActivity || task.agentActivity))).length,
        taskCount: work.length};
    });
    const labels = new Map();
    for (const actor of actors) labels.set(actor.label, (labels.get(actor.label) || 0) + 1);
    for (const actor of actors) {
      actor.baseLabel = actor.label;
      if (labels.get(actor.label) > 1) actor.label += ` · ${actor.sourceId}/${actor.localId}`;
    }
    actors.sort((a, b) => actorPriority(a) - actorPriority(b) || a.id.localeCompare(b.id));
    const latest = tasks.reduce((value, task) => Math.max(value, task._milliseconds), -Infinity);
    const publicTasks = tasks.map(({_milliseconds, _observationMilliseconds, _observationExpired, _claudeLifecycle, _claudeStartedAt, _entity, _telemetryRows,
      _hostAuthoritative, _hostContextActionKnown, _invokedAt, ...task}) => {
      const row = {...task, sourceId: _entity.sourceId, sourceKind: _entity.sourceKind};
      if (_invokedAt) row.invokedAt = _invokedAt;
      return row;
    });
    return {
      project,
      actors,
      entityCount: actors.length,
      tasks: publicTasks,
      counts,
      latestEventAt: Number.isFinite(latest) ? new Date(latest).toISOString() : null,
    };
  }

  function taskRole(task, owner) {
    if (!task || !owner || owner.kind === 'executor') return null;
    let role = task.searchScope ? 'research' : task.activityRole;
    if (!role) role = {reason: 'analysis', review: 'review', search: 'research'}[task.action] || null;
    if (owner.kind === 'mcp' && (role !== 'research' || task.searchScope !== 'web')) return null;
    return role;
  }

  function taskTime(task) {
    const value = Date.parse(task?.time);
    return Number.isFinite(value) ? value : -Infinity;
  }

  function newerTask(left, right) {
    return taskTime(right) - taskTime(left) || left.id.localeCompare(right.id);
  }

  function visibleAttention(task) {
    return task.state === 'unknown' || ATTENTION_STATES.has(task.state);
  }

  function meaningfulUnknown(task, owner) {
    if (task.state !== 'unknown') return true;
    if (task.kind === 'call' || task.hostExecutionId) return true;
    if (task.activityRole || task.searchScope) return true;
    return owner.kind !== 'agent' || task.action !== 'execute';
  }

  function latestInvocation(tasks, invocationOrder) {
    return [...tasks].sort((left, right) => {
      const timeDifference = Date.parse(right.invokedAt) - Date.parse(left.invokedAt);
      if (timeDifference) return timeDifference;
      const leftOrder = Number.isFinite(invocationOrder.get(left.id)) ? invocationOrder.get(left.id) : -Infinity;
      const rightOrder = Number.isFinite(invocationOrder.get(right.id)) ? invocationOrder.get(right.id) : -Infinity;
      return leftOrder === rightOrder ? left.id.localeCompare(right.id) : rightOrder - leftOrder;
    })[0];
  }

  function displayTask(tasks, owner, latest) {
    const attention = tasks.find(task => task === latest && visibleAttention(task) && meaningfulUnknown(task, owner));
    if (attention) return attention;
    if (owner.sourceKind === 'codex-cli' && tasks.some(task => task.sessionActivity)) {
      const nativeCall = tasks.find(task => task.kind === 'call' && ACTIVE_STATES.has(task.state));
      if (nativeCall) return nativeCall;
    }
    if (owner.sourceKind === 'claude-cli' && tasks.some(task => task.agentActivity)) {
      const concrete = tasks.find(task => task.kind === 'call' && !task.agentActivity && ACTIVE_STATES.has(task.state));
      if (concrete) return concrete;
    }
    const active = tasks.find(task => ['started', 'running', 'canceling'].includes(task.state));
    if (active) return active;
    return tasks.find(task => ['returned', 'succeeded'].includes(task.state)) || null;
  }

  function latestConcreteTask(tasks, ownerId) {
    if (typeof ownerId !== 'string') return null;
    return recentTasks((Array.isArray(tasks) ? tasks : []).filter(task =>
      task?.actorId === ownerId && task.kind === 'call' && task.sessionActivity !== true && task.agentActivity !== true))[0] || null;
  }

  function sprite(owner, task, displayType, role = null, invocationTask = task) {
    const genericHost = Boolean(task.hostExecutionId && task.hostActionKnown !== true);
    const row = {...owner,
      id: displayType === 'role'
        ? `role:${role}:${task.searchScope || 'generic'}:${encodeURIComponent(owner.id)}` : owner.id,
      ownerId: owner.id,
      displayType,
      taskId: task.id,
      invocationId: invocationTask.id,
      invokedAt: invocationTask.invokedAt,
      state: task.state,
      room: visibleAttention(task) ? 'attention' : genericHost ? 'execution'
        : displayType === 'role' ? ROLE_ROOMS[role] : task.room,
      sourceRoom: genericHost ? 'execution' : task.sourceRoom,
      action: genericHost ? 'execute' : task.action,
      time: task.time,
      provenance: task.provenance,
      telemetry: task.telemetry || {},
      tool: task.tool ?? null,
      activityRole: task.activityRole ?? null,
      searchScope: task.searchScope ?? null,
      parentTaskId: task.parentTaskId ?? null,
      jobId: task.jobId ?? null,
      ...(task.skill ? {skill: {...task.skill}} : {}),
      ...(task.hostExecutionId ? {hostExecutionId: task.hostExecutionId,
        hostActionKnown: task.hostActionKnown === true} : {})};
    if (displayType === 'role') row.role = role;
    return row;
  }

  function idleSprite(owner, task, invocationTask = task) {
    return {...owner,
      ownerId: owner.id,
      displayType: 'actor',
      taskId: task.id,
      invocationId: invocationTask.id,
      invokedAt: invocationTask.invokedAt,
      state: 'idle',
      room: 'waiting',
      action: 'idle',
      telemetry: task.telemetry || {},
      tool: task.tool ?? null,
      activityRole: task.activityRole ?? null,
      searchScope: task.searchScope ?? null,
      parentTaskId: task.parentTaskId ?? null,
      ...(task.skill ? {skill: {...task.skill}} : {})};
  }

  function waitingSprite(owner, task, displayType, role = null, invocationTask = task) {
    const row = sprite(owner, task, displayType, role, invocationTask);
    row.state = 'idle';
    row.room = 'waiting';
    row.action = 'idle';
    return row;
  }

  function scene(canonical, {invocationOrder = new Map()} = {}) {
    if (!canonical || !Array.isArray(canonical.actors) || !Array.isArray(canonical.tasks)) {
      return {sprites: [], roles: []};
    }
    const owners = new Map(canonical.actors.map(owner => [owner.id, owner]));
    const work = new Map(canonical.actors.map(owner => [owner.id, []]));
    for (const task of canonical.tasks) {
      if (!owners.has(task.actorId)) continue;
      work.get(task.actorId).push(task);
    }

    const sprites = [];
    const roles = [];
    for (const [ownerId, tasks] of work) {
      const owner = owners.get(ownerId);
      const ordered = [...tasks].filter(task => task.invokedAt).sort(newerTask);
      const latest = ordered[0];
      const byRole = new Map();
      for (const task of ordered) {
        const role = taskRole(task, owner);
        if (!role) continue;
        const key = `${role}:${task.searchScope || 'generic'}`;
        if (!byRole.has(key)) byRole.set(key, {tasks: [], role});
        byRole.get(key).tasks.push(task);
      }
      for (const {tasks: roleTasks, role} of byRole.values()) {
        const task = displayTask(roleTasks, owner, latest);
        if (!task) continue;
        const invocationTask = latestInvocation(roleTasks, invocationOrder);
        const row = ['returned', 'succeeded'].includes(task.state)
          ? waitingSprite(owner, task, 'role', role, invocationTask)
          : sprite(owner, task, 'role', role, invocationTask);
        roles.push(row);
        sprites.push(row);
      }

      const bodyTasks = ordered.filter(task => !taskRole(task, owner));
      const body = displayTask(bodyTasks, owner, latest);
      const invocationTask = bodyTasks.length ? latestInvocation(bodyTasks, invocationOrder) : null;
      if (body) sprites.push(['returned', 'succeeded'].includes(body.state)
        ? idleSprite(owner, body, invocationTask) : sprite(owner, body, 'actor', null, invocationTask));
    }
    return {sprites, roles};
  }

  function changes(previous, current, {now = Date.now()} = {}) {
    if (!previous || !current || previous.project !== current.project
        || !Array.isArray(previous.tasks) || !Array.isArray(current.tasks)) return [];
    const known = new Map(previous.tasks.map(task => [task.id, task]));
    const result = [];
    for (const task of current.tasks) {
      const before = known.get(task.id), time = Date.parse(task.time);
      if (!Number.isFinite(time) || !Number.isFinite(now) || (!before && time < now - 15000) || time > now + 5000
          || (before && before.state === task.state && before.action === task.action)) continue;
      result.push({...Object.fromEntries(['id', 'actorId', 'kind', 'label', 'tool', 'action', 'state', 'time',
        'provenance', 'room', 'sourceRoom'].map(key => [key, task[key]])), noticedAt: new Date(now).toISOString()});
    }
    return result.sort((a, b) => Date.parse(a.time) - Date.parse(b.time) || a.id.localeCompare(b.id));
  }

  function recentTasks(tasks, {order = new Map()} = {}) {
    if (!Array.isArray(tasks)) return [];
    const ranks = order && typeof order.get === 'function' ? order : new Map();
    return [...tasks].sort((left, right) => {
      const timeDifference = taskTime(right) - taskTime(left);
      if (timeDifference) return timeDifference;
      const leftRank = ranks.get(left?.id);
      const rightRank = ranks.get(right?.id);
      const rankDifference = (Number.isFinite(rightRank) ? rightRank : -Infinity)
        - (Number.isFinite(leftRank) ? leftRank : -Infinity);
      if (rankDifference) return rankDifference;
      return String(left?.id ?? '').localeCompare(String(right?.id ?? ''));
    });
  }

  function emptyResumeChanges() {
    return {newTasks: [], completed: [], attention: [], changed: [], missing: []};
  }

  function resumeChanges(previous, current, {since, now = Date.now()} = {}) {
    const result = emptyResumeChanges();
    if (!previous || !current || previous.project !== current.project
        || !Array.isArray(previous.tasks) || !Array.isArray(current.tasks)
        || !Number.isFinite(since) || !Number.isFinite(now) || since > now) return result;
    const beforeById = new Map(previous.tasks.filter(task => task && typeof task.id === 'string')
      .map(task => [task.id, task]));
    const currentIds = new Set();
    const attentionStates = new Set([...ATTENTION_STATES, 'unknown']);
    const doneStates = new Set(['returned', 'succeeded']);

    for (const task of current.tasks) {
      if (!task || typeof task.id !== 'string') continue;
      currentIds.add(task.id);
      const transitionTime = taskTime(task);
      if (!Number.isFinite(transitionTime) || transitionTime < since || transitionTime > now) continue;
      const before = beforeById.get(task.id);
      const row = {...task};
      if (!before) result.newTasks.push(row);
      else if (before.state !== task.state || before.action !== task.action) result.changed.push(row);

      const wasDone = before && doneStates.has(before.state);
      if (doneStates.has(task.state) && !wasDone) result.completed.push(row);
      const wasAttention = before && attentionStates.has(before.state);
      if (attentionStates.has(task.state) && !wasAttention) result.attention.push(row);
    }
    for (const task of previous.tasks) {
      if (task && typeof task.id === 'string' && !currentIds.has(task.id)) result.missing.push({...task});
    }
    for (const rows of Object.values(result)) rows.sort((left, right) =>
      taskTime(left) - taskTime(right) || left.id.localeCompare(right.id));
    return result;
  }

  const SPEECH_BY_STATE = {
    succeeded: {kind: 'complete', tone: 'success', text: '작업 끝!', priority: 2, durationMs: 4000},
    returned: {kind: 'returned', tone: 'info', text: '응답 보냈어요', priority: 1, durationMs: 4000},
    failed: {kind: 'failed', tone: 'danger', text: '앗, 문제가 생겼어요!', priority: 3, durationMs: 6000},
    timed_out: {kind: 'timed_out', tone: 'warning', text: '제한 시간을 넘겼어요', priority: 2, durationMs: 6000},
    canceled: {kind: 'canceled', tone: 'neutral', text: '작업이 취소됐어요', priority: 2, durationMs: 6000},
    interrupted: {kind: 'interrupted', tone: 'warning', text: '작업이 중단됐어요', priority: 2, durationMs: 6000},
    unknown: {kind: 'unknown', tone: 'warning', text: '결과 확인이 필요해요', priority: 2, durationMs: 6000},
  };
  const SPEECH_BY_ACTION = {
    read: '자료 확인 중!', search: '검색 중!', reason: '검토 중!', code: '코드 작성 중!',
    build: '빌드 중!', test: '테스트 중!', review: '검토 중!', execute: '실행 중!',
    commit: '커밋 준비 중!', push: '전송 중!',
  };

  function skillLabel(task) {
    return task?.skill?.name || task?.skill?.id || null;
  }

  function skillSpeech(task, before) {
    const label = skillLabel(task);
    if (!label) return null;
    const terminal = {
      returned: {kind: 'skill-returned', tone: 'info', text: `${label} 호출 반환`, priority: 1, durationMs: 4000},
      failed: {kind: 'skill-failed', tone: 'danger', text: `${label} 스킬 실패`, priority: 3, durationMs: 6000},
      timed_out: {kind: 'skill-timed-out', tone: 'warning', text: `${label} 시간 초과`, priority: 2, durationMs: 6000},
      canceled: {kind: 'skill-canceled', tone: 'neutral', text: `${label} 스킬 취소`, priority: 2, durationMs: 6000},
      interrupted: {kind: 'skill-interrupted', tone: 'warning', text: `${label} 스킬 중단`, priority: 2, durationMs: 6000},
      unknown: {kind: 'skill-unknown', tone: 'warning', text: `${label} 결과 확인 필요`, priority: 2, durationMs: 6000},
    };
    if (terminal[task.state] && before?.state !== task.state) return terminal[task.state];
    if (['started', 'running'].includes(task.state)
        && (!before || !['started', 'running'].includes(before.state))) {
      return {kind: 'skill-start', tone: 'info', text: `${label} 스킬 발동!`, priority: 2, durationMs: 4000};
    }
    return null;
  }

  function invokedTask(task) {
    const invoked = Date.parse(task?.invokedAt);
    const transitioned = Date.parse(task?.time);
    return task && typeof task.id === 'string' && typeof task.actorId === 'string'
      && TASK_KINDS.has(task.kind) && Object.hasOwn(ACTION_ROOMS, task.action)
      && ['observed', 'reported'].includes(task.provenance)
      && Number.isFinite(invoked) && Number.isFinite(transitioned) && invoked <= transitioned;
  }

  function speechFor(task, before) {
    const skill = skillSpeech(task, before);
    if (skill) return skill;
    if (Object.hasOwn(SPEECH_BY_STATE, task.state) && before?.state !== task.state) {
      return SPEECH_BY_STATE[task.state];
    }
    if (!['started', 'running'].includes(task.state)) return null;
    if (!before || !['started', 'running'].includes(before.state)) {
      return {kind: 'start', tone: 'info', text: '작업 시작!', priority: 1, durationMs: 4000};
    }
    if (before.action === task.action) return null;
    const genericHost = task.hostExecutionId && task.hostActionKnown !== true;
    return {kind: 'action', tone: 'info',
      text: genericHost ? '작업 진행 중!' : SPEECH_BY_ACTION[task.action] || '작업 진행 중!',
      priority: 1, durationMs: 4000};
  }

  function speechEvents(previous, current, {now = Date.now()} = {}) {
    if (!previous || !current || previous.project !== current.project
        || !Array.isArray(previous.tasks) || !Array.isArray(current.tasks)
        || !Number.isFinite(now)) return [];
    const beforeById = new Map(previous.tasks.filter(task => task && typeof task.id === 'string')
      .map(task => [task.id, task]));
    const currentById = new Map(current.tasks.filter(task => task && typeof task.id === 'string')
      .map(task => [task.id, task]));
    const result = [];
    for (const delta of changes(previous, current, {now})) {
      const task = currentById.get(delta.id);
      if (!invokedTask(task)) continue;
      const before = beforeById.get(task.id);
      if (before && taskTime(task) < taskTime(before)) continue;
      const speech = speechFor(task, before);
      if (speech) result.push({...task, speech: {...speech}});
    }
    return result;
  }

  // Display defaults describe the object, not the action it happens to perform.
  // A role sprite remains a presentation of its owner, not a new AI identity.
  function characterJob(actor) {
    if (actor?.kind === 'mcp') return {id: 'support', label: '도구 담당', mark: 'MCP', equipment: '연결된 MCP 도구'};
    if (actor?.kind === 'executor') return {id: 'operator', label: '운영자', mark: '>_', equipment: '명령 실행기'};
    if (actor?.sourceKind === 'chatgpt') return {id: 'planner', label: '설계자', mark: 'P', equipment: '채팅 작업 기록'};
    if (actor?.kind === 'agent') return {id: 'developer', label: '개발자', mark: '</>', equipment: '에이전트 작업 기록'};
    return {id: 'observer', label: '기록 담당', mark: '[]', equipment: '수집된 작업 기록'};
  }

  function hostExecutionReference(task) {
    return typeof task?.hostExecutionId === 'string' && JOB_ID.test(task.hostExecutionId)
      ? `실행 ${task.hostExecutionId.slice(0, 8)}` : null;
  }

  function hostCommandLabel(task) {
    const labels = {
      'Host command': '호스트 명령 · 종류 미수집',
      'Python command': 'Python 실행',
      'Shell command': '셸 실행',
      'Codex host task': 'Codex 작업 위임',
    };
    return labels[task?.commandLabel] || task?.commandLabel || '명령 종류 미수집';
  }

  function hostResultSummary(task) {
    const exitCode = Number.isInteger(task?.result?.exit_code) ? task.result.exit_code : null;
    if (task?.state === 'succeeded') {
      const code = exitCode == null ? '' : ` 종료 코드 ${exitCode}.`;
      return `명령 종료가 확인됐습니다.${code} 추가 업무 결과는 수집되지 않았습니다.`;
    }
    const outcomes = {failed: '실패', timed_out: '시간 초과', canceled: '취소',
      interrupted: '중단', returned: '요청 반환'};
    if (task?.state === 'returned') return '도구 요청이 반환됐습니다. 실행 성공 여부는 별도 실행 결과에서 확인하세요.';
    if (!outcomes[task?.state]) return null;
    return `${outcomes[task.state]} 상태가 기록됐습니다.${exitCode == null ? '' : ` 종료 코드 ${exitCode}.`}`;
  }

  function recordedAnswer({actor, task, tasks = [], question, stale = false, paused = false, archived = false}) {
    const ownerId = actor?.id || task?.actorId;
    const related = ownerId ? recentTasks(tasks.filter(row => row.actorId === ownerId)) : [];
    if (question === 'history') return `현재 조회 범위에 이 담당자의 일감 ${related.length}개가 있습니다. 관련 기록에서 작업을 선택해 결과와 원본 출력을 확인하세요.`;
    const outcomes = {succeeded: '성공', failed: '실패', timed_out: '시간 초과', canceled: '취소', interrupted: '중단', returned: '요청 반환'};
    const resultText = result => result.hostExecutionId ? hostResultSummary(result) : (() => {
      const code = Number.isInteger(result.result?.exit_code) ? ` 종료 코드 ${result.result.exit_code}.` : '';
      if (result.skill && result.state === 'returned') {
        return '스킬 호출이 반환됐습니다. 스킬이 지시한 워크플로 전체의 완료가 확인된 것은 아닙니다.';
      }
      if (result.state === 'returned') return result.agentActivity
        ? '하위 에이전트의 반환 상태가 관측됐습니다. 전체 업무의 성공을 뜻하지 않습니다.'
        : '도구 요청이 반환됐습니다. 실행 성공 여부는 별도 실행 결과에서 확인하세요.';
      return outcomes[result.state] ? `${outcomes[result.state]} 상태가 기록됐습니다.${code}` : null;
    })();
    const contextText = result => {
      const purpose = result.label || '선택한 작업';
      const skill = result.skill ? ` 스킬 ${result.skill.name || result.skill.id} (${result.skill.id}) · ${
        result.skill.provenance === 'observed' ? '직접 관측' : '보고됨'}.` : '';
      if (!result.hostExecutionId) return `${purpose} · ${result.provenance === 'observed' ? '직접 관측' : '보고됨'}.${skill} `;
      const reference = hostExecutionReference(result);
      const missingPurpose = result.workContext?.labelSource === 'unavailable';
      const purposeText = missingPurpose
        ? `작업 목적 미수집. 이 실행을 요청한 업무명은 당시 수집되지 않았습니다.${reference ? ` ${reference} 기록입니다` : ''}`
        : purpose;
      const purposeEvidence = result.workContext?.labelSource === 'reported'
        ? '업무 설명 보고됨' : result.workContext?.labelSource === 'observed'
          ? '업무 설명 직접 관측' : '업무 설명 미수집';
      const executionEvidence = result.provenance === 'observed'
        ? '실행 상태 직접 관측' : '실행 상태 보고됨';
      const phase = result.hostPhase ? ` 관측 단계: ${hostPhaseLabel(result.hostPhase)}.` : '';
      return `${purposeText}.${missingPurpose ? '' : ` ${purposeEvidence}.`} 명령 종류: ${hostCommandLabel(result)}.${phase} ${executionEvidence}. `;
    };
    if (!task) return question === 'result' ? '수신된 완료 결과가 없습니다.' : '이 담당자의 작업 기록이 없습니다.';
    if (question === 'result') {
      const selectedOutcome = resultText(task);
      if (selectedOutcome) return contextText(task) + selectedOutcome;
      const previous = related.find(row => row.id !== task.id && outcomes[row.state]);
      const selected = `${contextText(task)}선택한 작업의 완료 결과는 아직 없습니다.`;
      return previous ? `${selected} 이전 완료 작업: ${contextText(previous)}${resultText(previous)}` : selected;
    }
    const result = task;
    const prefix = contextText(result);
    const outcome = resultText(result);
    if (stale || paused || archived) return prefix + '마지막 기록을 보여드리고 있습니다. 현재 진행 상태나 중단 원인은 확인할 수 없습니다.';
    if (question === 'stop') {
      if (outcome) return prefix + outcome + (['failed', 'timed_out', 'interrupted'].includes(result.state)
        ? ' 구체적인 원인은 원본 로그에서 확인해야 합니다.' : ' 이 기록만으로 추가 원인을 알 수 없습니다.');
      if (result.state === 'unknown') return prefix + '종료 결과와 중단 원인을 확인할 수 없습니다.';
      if (['prepared', 'queued'].includes(result.state)) return prefix + '실행 전 대기 기록입니다. 대기 원인은 수신되지 않았습니다.';
      return prefix + '중단됐다는 기록은 없습니다. 화면의 이동 정지와 실제 작업 중단은 별개입니다.';
    }
    const actions = {read: '읽기', search: '검색', reason: '판단', code: '코드 작성', build: '빌드', test: '검사', review: '검토', execute: '실행', commit: '커밋', push: '전송'};
    if (outcome) return prefix + '이전 기록입니다. ' + outcome;
    if (result.state === 'unknown') return prefix + '마지막 기록 이후의 진행·종료 상태를 확인할 수 없습니다.';
    if (['prepared', 'queued'].includes(result.state)) return prefix + '실행 전 대기 상태가 기록됐습니다.';
    if (result.state === 'canceling') return prefix + '취소 처리 중으로 기록됐습니다. 종료 결과는 아직 없습니다.';
    return prefix + `${result.agentActivity ? '하위 에이전트 활동' : result.hostExecutionId && result.hostActionKnown !== true ? '세부 행동 미수신' : actions[result.action] || '작업'} · 진행 중으로 기록됐습니다. 완료 결과는 아직 없습니다.`;
  }

  return {snapshot, scene, changes, recentTasks, latestConcreteTask, resumeChanges, speechEvents, characterJob, recordedAnswer,
    hostPhaseLabel, hostExecutionReference, hostCommandLabel, hostResultSummary};
}));
