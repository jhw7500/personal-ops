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
  const ACTIVE_STATES = new Set(['started', 'running', 'queued', 'canceling']);
  const ATTENTION_STATES = new Set(['failed', 'timed_out', 'canceled', 'interrupted']);
  const TERMINAL_STATES = new Set(['returned', 'succeeded', ...ATTENTION_STATES]);
  const ACTION_ROOMS = {read: 'analysis', search: 'analysis', reason: 'analysis', code: 'coding', build: 'testing',
    test: 'testing', review: 'review', execute: 'execution', commit: 'review', push: 'review'};
  const SOURCE_KINDS = new Set(['mcp', 'chatgpt', 'codex-cli', 'claude-cli']);
  const METADATA_ID = /^[A-Za-z0-9_.:+/-]+$/;
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
    ['start_command', 'execution'],
    ['read_project_memory', 'analysis'],
    ['update_project_memory', 'review'],
    ['read_project_progress', 'analysis'],
    ['update_project_progress', 'review'],
    ['list_execution_profiles', 'analysis'],
  ]);

  function sourceRoomForTool(tool) {
    if (typeof tool !== 'string') return null;
    if (tool.startsWith('git_')) return 'review';
    return TOOL_ROOMS.get(tool) || null;
  }

  function timestamp(value) {
    if (typeof value !== 'string' || value.length === 0) return null;
    const milliseconds = Date.parse(value);
    if (!Number.isFinite(milliseconds)) return null;
    return {milliseconds, iso: new Date(milliseconds).toISOString()};
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
        _entity: identity(LEGACY_SOURCE, 'mcp', 'coding-mcp', 'mcp', '코딩 MCP'),
        _milliseconds: parsed.milliseconds,
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

  function normalizeJobs(project, jobs) {
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
        _entity: identity(LEGACY_SOURCE, 'mcp', 'commands', 'executor', '명령 실행기'),
        _milliseconds: times.display.milliseconds,
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
    return result;
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

  // Adapters provide explicit identities; CLI/model/agent identities are never inferred from tool names.
  function normalizeRecords(project, records, now) {
    const selected = new Map();
    const starts = new Map();
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
      if (ACTIVE_STATES.has(state) && now >= Math.min(expiry?.milliseconds ?? deadline, deadline)) state = 'unknown';
      const sourceRoom = ACTION_ROOMS[row.action];
      const room = state === 'returned' || state === 'succeeded' ? 'done'
        : ATTENTION_STATES.has(state) ? 'attention'
          : state === 'queued' || state === 'unknown' ? 'waiting' : sourceRoom;
      return {id, kind: row.task_kind, actorId: actor.id, label: typeof row.task_label === 'string'
        && row.task_label ? row.task_label.slice(0, 200) : row.action, action: row.action,
        state, room, sourceRoom, time: parsed.iso, tool: null, jobId: null,
        provenance: row.provenance === 'observed' ? 'observed' : 'reported',
        telemetry: telemetryFor(metadata.get(id)),
        replayable: TERMINAL_STATES.has(state) && starts.has(id) && starts.get(id) <= parsed.milliseconds,
        parentTaskId: typeof row.parent_task_id === 'string' && CALL_ID.test(row.parent_task_id)
          ? `task:${encodeURIComponent(typeof row.parent_source_id === 'string' && CALL_ID.test(row.parent_source_id)
            ? row.parent_source_id : row.source_id)}:${encodeURIComponent(
            typeof row.parent_actor_id === 'string' && CALL_ID.test(row.parent_actor_id) ? row.parent_actor_id : row.actor_id
          )}:${encodeURIComponent(row.parent_task_id)}` : null,
        _entity: actor, _milliseconds: parsed.milliseconds, _telemetryRows: metadata.get(id)};
    });
  }

  function actorPriority(actor) {
    if (actor.state === 'running' || actor.state === 'started' || actor.state === 'canceling') return 0;
    if (actor.state === 'queued') return 1;
    if (actor.state === 'unknown') return 2;
    return 3;
  }

  function snapshot({project, jobs = [], events = [], records = [], objects = [], now = Date.now()} = {}) {
    const nowMilliseconds = typeof now === 'number' && Number.isFinite(now)
      ? now : Date.parse(now);
    const time = Number.isFinite(nowMilliseconds) ? nowMilliseconds : Date.now();
    const tasks = normalizeEvents(project, events,
      time).concat(normalizeJobs(project, jobs), normalizeRecords(project, records, time),
    );
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
      const pending = work.find(task => ACTIVE_STATES.has(task.state) || task.state === 'unknown');
      const current = pending || latest;
      return {...latest._entity, project, id, state: pending ? pending.state : 'idle',
        room: pending ? pending.room : 'waiting', sourceRoom: current.sourceRoom,
        action: pending ? pending.action : 'idle', taskId: pending ? pending.id : null,
        lastTaskId: latest.id, time: latest.time, jobId: pending ? pending.jobId : null,
        provenance: current.provenance,
        telemetry: telemetryFor(work.flatMap(task => task._telemetryRows || [])),
        activeCount: work.filter(task => ACTIVE_STATES.has(task.state)).length,
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
    return {
      project,
      actors: actors.slice(0, 24),
      entityCount: actors.length,
      tasks: tasks.map(({_milliseconds, _entity, _telemetryRows, ...task}) => ({...task,
        sourceId: _entity.sourceId, sourceKind: _entity.sourceKind})),
      counts,
      latestEventAt: Number.isFinite(latest) ? new Date(latest).toISOString() : null,
    };
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

  return {snapshot, changes};
}));
