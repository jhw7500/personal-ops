'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {scene, snapshot} = require('../model.js');

const project = 'pim-check';
const now = Date.parse('2026-10-15T04:10:00Z');

function event(overrides = {}) {
  return {project, call_id: 'mcp-call', tool: 'read_file', phase: 'started',
    time: '2026-10-15T04:00:00Z', ...overrides};
}

function job(overrides = {}) {
  return {project, job_id: 'a'.repeat(32), state: 'running', label: '검사 실행',
    created_at: '2026-10-15T04:01:00Z', started_at: '2026-10-15T04:01:02Z',
    finished_at: null, exit_code: null, ...overrides};
}

function host(overrides = {}) {
  return {project, operation_id: 'b'.repeat(32), kind: 'codex', state: 'running',
    created_at: '2026-10-15T04:02:00Z', started_at: '2026-10-15T04:02:03Z',
    finished_at: null, result: null, termination_verified: false,
    authorization: {kind: 'host_access', grant_id: 'c'.repeat(32), expires_at: '2026-10-15T05:00:00Z'},
    agent_metadata: {profile_id: 'codex-review', model: 'gpt-test', reasoning_effort: 'high',
      profile_digest: 'd'.repeat(64)}, ...overrides};
}

test('an MCP call enters work then returns to waiting without changing invocation recency', () => {
  const active = scene(snapshot({project, now, events: [event()]})).sprites[0];
  const completed = scene(snapshot({project, now, events: [
    event({phase: 'completed', time: '2026-10-15T04:00:08Z'}),
    event(),
  ]})).sprites[0];
  const laterPoll = scene(snapshot({project, now: now + 60000, events: [
    event({phase: 'completed', time: '2026-10-15T04:00:08Z'}),
    event(),
  ]})).sprites[0];

  assert.equal(active.id, 'entity:personal-ops-tunnel:coding-mcp');
  assert.equal(completed.id, active.id);
  assert.equal(completed.state, 'idle');
  assert.equal(completed.room, 'waiting');
  assert.equal(completed.invocationId, 'call:mcp-call');
  assert.equal(completed.invokedAt, '2026-10-15T04:00:00.000Z');
  assert.equal(laterPoll.invokedAt, completed.invokedAt);
});

test('jobs and host agents use their real start as invocation recency', () => {
  const view = scene(snapshot({project, now, hostObservedAt: '2026-10-15T04:09:59Z',
    jobs: [job()], hostExecutions: [host()]}));
  const command = view.sprites.find(row => row.ownerId === 'entity:personal-ops-tunnel:commands');
  const codex = view.sprites.find(row => row.ownerId === 'entity:personal-ops-agent:codex-review');

  assert.equal(command.invocationId, `job:${'a'.repeat(32)}`);
  assert.equal(command.invokedAt, '2026-10-15T04:01:02.000Z');
  assert.equal(codex.invocationId, `task:personal-ops-agent:codex-review:${'b'.repeat(32)}`);
  assert.equal(codex.invokedAt, '2026-10-15T04:02:03.000Z');
});

test('a legacy running job without started_at is admitted at its first observation time', () => {
  const row = scene(snapshot({project, now, jobs: [job({started_at: null})]})).sprites[0];
  assert.equal(row.ownerId, 'entity:personal-ops-tunnel:commands');
  assert.equal(row.state, 'running');
  assert.equal(row.room, 'execution');
  assert.equal(row.invocationId, `job:${'a'.repeat(32)}`);
  assert.equal(row.invokedAt, '2026-10-15T04:10:00.000Z');
});

test('a terminal host observation preserves an earlier detailed agent start', () => {
  const taskId = 'b'.repeat(32);
  const records = [{
    schema_version: 1, project, source_id: 'personal-ops-agent', source_kind: 'codex-cli',
    actor_id: 'codex-review', actor_kind: 'agent', actor_label: 'Codex', task_id: taskId,
    task_kind: 'agent', task_label: 'review', action: 'code', state: 'running',
    time: '2026-10-15T04:02:01Z', provenance: 'reported',
  }];
  const terminal = host({state: 'succeeded', started_at: null, finished_at: '2026-10-15T04:02:09Z',
    result: {exit_code: 0, duration_ms: 8000, stdout_bytes: 10, stderr_bytes: 0},
    termination_verified: true});
  const result = scene(snapshot({project, now, records, hostObservedAt: '2026-10-15T04:09:59Z',
    hostExecutions: [terminal]})).sprites[0];

  assert.equal(result.state, 'idle');
  assert.equal(result.invokedAt, '2026-10-15T04:02:01.000Z');
});

test('terminal observation is a bounded fallback when actual start is unavailable', () => {
  const result = scene(snapshot({project, now, jobs: [job({state: 'succeeded', started_at: null,
    finished_at: '2026-10-15T04:01:09Z', exit_code: 0})]})).sprites[0];
  assert.equal(result.state, 'idle');
  assert.equal(result.room, 'waiting');
  assert.equal(result.invokedAt, '2026-10-15T04:01:09.000Z');
});

test('queued work and prepared host plans do not admit characters before invocation', () => {
  const result = scene(snapshot({project, now, hostObservedAt: '2026-10-15T04:09:59Z',
    jobs: [job({state: 'queued', started_at: null})],
    hostExecutions: [host({state: 'prepared', started_at: null, termination_verified: null})]}));
  assert.deepEqual(result, {sprites: [], roles: []});
});

test('canonical scene retains every called actor so room admission can choose the newest', () => {
  const records = Array.from({length: 25}, (_, index) => ({
    schema_version: 1,
    project,
    source_id: 'codex-local',
    source_kind: 'codex-cli',
    actor_id: `agent-${String(index).padStart(2, '0')}`,
    actor_kind: 'agent',
    actor_label: `Agent ${index}`,
    task_id: `task-${index}`,
    task_kind: 'agent',
    task_label: `작업 ${index}`,
    action: 'code',
    state: 'succeeded',
    time: new Date(Date.parse('2026-10-15T04:00:00Z') + index * 1000).toISOString(),
    provenance: 'reported',
  }));

  const canonical = snapshot({project, now, records});
  const view = scene(canonical);
  assert.equal(canonical.entityCount, 25);
  assert.equal(canonical.actors.length, 25);
  assert.equal(canonical.tasks.length, 25);
  assert.equal(view.sprites.length, 25);
  assert.ok(view.sprites.some(row => row.ownerId === 'entity:codex-local:agent-24'));
});

test('a newer completed call updates recency without hiding older active work on the same actor', () => {
  const base = {
    schema_version: 1, project, source_id: 'codex-local', source_kind: 'codex-cli',
    actor_id: 'session-1', actor_kind: 'agent', actor_label: 'Codex', task_kind: 'agent',
    task_label: '작업', action: 'code', provenance: 'reported',
  };
  const records = [
    {...base, task_id: 'active-a', state: 'running', time: '2026-10-15T04:09:50Z', model: 'model-a'},
    {...base, task_id: 'quick-b', state: 'running', time: '2026-10-15T04:09:55Z', model: 'model-b'},
    {...base, task_id: 'quick-b', state: 'succeeded', time: '2026-10-15T04:10:00Z', model: 'model-b'},
  ];

  const row = scene(snapshot({project, now, records})).sprites[0];
  assert.equal(row.taskId, 'task:codex-local:session-1:active-a');
  assert.equal(row.state, 'running');
  assert.equal(row.action, 'code');
  assert.equal(row.telemetry.model.value, 'model-a');
  assert.equal(row.invocationId, 'task:codex-local:session-1:quick-b');
  assert.equal(row.invokedAt, '2026-10-15T04:09:55.000Z');
});

test('a newer meaningful unknown remains attention while older work is active', () => {
  const base = {
    schema_version: 1, project, source_id: 'codex-local', source_kind: 'codex-cli',
    actor_id: 'session-1', actor_kind: 'agent', actor_label: 'Codex', task_kind: 'agent',
    task_label: '작업', action: 'code', provenance: 'reported',
  };
  const row = scene(snapshot({project, now, records: [
    {...base, task_id: 'active-a', state: 'running', time: '2026-10-15T04:09:50Z'},
    {...base, task_id: 'uncertain-b', state: 'unknown', time: '2026-10-15T04:09:55Z'},
  ]})).sprites[0];

  assert.equal(row.taskId, 'task:codex-local:session-1:uncertain-b');
  assert.equal(row.state, 'unknown');
  assert.equal(row.room, 'attention');
});

test('canonical tasks serialize invocation timestamps for every eligible concurrent call', () => {
  const base = {
    schema_version: 1, project, source_id: 'codex-local', source_kind: 'codex-cli',
    actor_id: 'session-1', actor_kind: 'agent', actor_label: 'Codex', task_kind: 'agent',
    task_label: '작업', action: 'code', provenance: 'reported',
  };
  const canonical = snapshot({project, now, records: [
    {...base, task_id: 'active-a', state: 'running', time: '2026-10-15T04:09:50Z'},
    {...base, task_id: 'quick-b', state: 'running', time: '2026-10-15T04:09:55Z'},
    {...base, task_id: 'quick-b', state: 'succeeded', time: '2026-10-15T04:10:00Z'},
    {...base, task_id: 'queued-c', state: 'queued', time: '2026-10-15T04:09:58Z'},
  ]});
  const serialized = JSON.parse(JSON.stringify(canonical));
  const byId = new Map(serialized.tasks.map(task => [task.id, task]));

  assert.equal(byId.get('task:codex-local:session-1:active-a').invokedAt, '2026-10-15T04:09:50.000Z');
  assert.equal(byId.get('task:codex-local:session-1:quick-b').invokedAt, '2026-10-15T04:09:55.000Z');
  assert.equal(Object.hasOwn(byId.get('task:codex-local:session-1:queued-c'), 'invokedAt'), false);
  assert.equal(Object.hasOwn(byId.get('task:codex-local:session-1:active-a'), '_invokedAt'), false);
});

test('caller observation order breaks equal-time invocation ties for an active body', () => {
  const base = {
    schema_version: 1, project, source_id: 'codex-local', source_kind: 'codex-cli',
    actor_id: 'session-1', actor_kind: 'agent', actor_label: 'Codex', task_kind: 'agent',
    task_label: '작업', action: 'code', provenance: 'reported',
  };
  const canonical = snapshot({project, now, records: [
    {...base, task_id: 'a-old', state: 'running', time: '2026-10-15T04:09:55Z', model: 'model-a'},
    {...base, task_id: 'z-new', state: 'running', time: '2026-10-15T04:09:55Z', model: 'model-b'},
    {...base, task_id: 'z-new', state: 'succeeded', time: '2026-10-15T04:10:00Z', model: 'model-b'},
  ]});
  const row = scene(canonical, {invocationOrder: new Map([
    ['task:codex-local:session-1:a-old', 1],
    ['task:codex-local:session-1:z-new', 2],
  ])}).sprites[0];

  assert.equal(row.taskId, 'task:codex-local:session-1:a-old');
  assert.equal(row.state, 'running');
  assert.equal(row.telemetry.model.value, 'model-a');
  assert.equal(row.invocationId, 'task:codex-local:session-1:z-new');
  assert.equal(row.invokedAt, '2026-10-15T04:09:55.000Z');
});
