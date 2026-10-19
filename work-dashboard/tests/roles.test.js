'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {scene, snapshot} = require('../model.js');

const project = 'tossApp';
const now = Date.parse('2026-10-15T01:10:30Z');

function record(overrides = {}) {
  return {
    schema_version: 1,
    project,
    source_id: 'codex-local',
    source_kind: 'codex-cli',
    actor_id: 'session-1',
    actor_kind: 'agent',
    actor_label: 'Codex',
    task_id: 'task-1',
    task_kind: 'agent',
    task_label: '작업',
    action: 'code',
    state: 'running',
    time: '2026-10-15T01:10:10Z',
    provenance: 'reported',
    model: 'model-a',
    ...overrides,
  };
}

function hostExecution(overrides = {}) {
  return {
    project,
    operation_id: 'a'.repeat(32),
    kind: 'codex',
    state: 'running',
    created_at: '2026-10-15T01:09:00Z',
    started_at: '2026-10-15T01:09:01Z',
    finished_at: null,
    result: null,
    termination_verified: false,
    authorization: {kind: 'host_access', grant_id: 'b'.repeat(32), expires_at: '2026-10-15T02:00:00Z'},
    agent_metadata: {profile_id: 'host-codex', model: 'model-a', reasoning_effort: 'high',
      profile_digest: 'c'.repeat(64)},
    ...overrides,
  };
}

function projected(records, options = {}) {
  return scene(snapshot({project, now, records, ...options}));
}

test('explicit phases and action defaults select the six role types', () => {
  const cases = [
    ['analysis', {action: 'reason'}],
    ['research', {action: 'search'}],
    ['planning', {activity_role: 'planning'}],
    ['debugging', {activity_role: 'debugging'}],
    ['review', {action: 'review'}],
    ['summary', {activity_role: 'summary'}],
  ];
  for (const [role, fields] of cases) {
    const view = projected([record(fields)]);
    assert.equal(view.sprites.length, 1);
    assert.equal(view.roles[0].role, role);
    assert.equal(view.roles[0].displayType, 'role');
    assert.equal(view.roles[0].ownerId, 'entity:codex-local:session-1');
    assert.equal(view.roles[0].taskId, 'task:codex-local:session-1:task-1');
  }
});

test('review and summary roles use the review room without changing canonical reason work', () => {
  for (const activity_role of ['review', 'summary']) {
    const canonical = snapshot({project, now, records: [record({action: 'reason', activity_role})]});
    const before = JSON.stringify(canonical);
    const role = scene(canonical).roles[0];
    assert.equal(canonical.tasks[0].room, 'analysis');
    assert.equal(canonical.tasks[0].action, 'reason');
    assert.deepEqual(canonical.counts, {active: 1, done: 0, attention: 0, unknown: 0, total: 1});
    assert.equal(role.room, 'review');
    assert.equal(role.action, 'reason');
    assert.equal(JSON.stringify(canonical), before);
  }
});

test('search scope controls research identity without inferring web from labels or generic tools', () => {
  const web = projected([record({action: 'search', activity_role: 'analysis', search_scope: 'web',
    task_label: 'ordinary', tool_name: 'web_search'})]).roles[0];
  const files = projected([record({action: 'search', search_scope: 'files', task_id: 'files'})]).roles[0];
  const generic = projected([record({action: 'search', task_id: 'generic', task_label: 'search the web',
    tool_name: 'web.run'})]).roles[0];
  assert.equal(web.id, 'role:research:web:entity%3Acodex-local%3Asession-1');
  assert.equal(files.id, 'role:research:files:entity%3Acodex-local%3Asession-1');
  assert.equal(generic.id, 'role:research:generic:entity%3Acodex-local%3Asession-1');
  assert.equal(generic.searchScope, null);
});

test('queued, prepared, and never-called agents do not create sprites', () => {
  assert.deepEqual(projected([record({state: 'queued'})]), {sprites: [], roles: []});
  const known = snapshot({project, now, records: [record({state: 'succeeded'})]});
  assert.deepEqual(scene(snapshot({project, now, objects: known.actors})), {sprites: [], roles: []});
  assert.deepEqual(scene(snapshot({project, now, hostObservedAt: '2026-10-15T01:10:20Z',
    hostExecutions: [hostExecution({state: 'prepared', started_at: null, termination_verified: null})]})),
  {sprites: [], roles: []});
});

test('role projection leaves canonical counts and entity collections unchanged', () => {
  const canonical = snapshot({project, now, records: [record({action: 'reason'})]});
  const before = JSON.stringify(canonical);
  const view = scene(canonical);
  assert.equal(view.sprites.length, 1);
  assert.equal(canonical.entityCount, 1);
  assert.equal(canonical.tasks.length, 1);
  assert.deepEqual(canonical.counts, {active: 1, done: 0, attention: 0, unknown: 0, total: 1});
  assert.equal(JSON.stringify(canonical), before);
});

test('concurrent code and reasoning keep one code body plus task-specific analysis telemetry', () => {
  const canonical = snapshot({project, now, records: [
    record({task_id: 'code', time: '2026-10-15T01:10:20Z', model: 'model-b'}),
    record({task_id: 'reason', action: 'reason', model: 'model-a'}),
  ]});
  const view = scene(canonical);
  assert.equal(canonical.entityCount, 1);
  assert.equal(canonical.tasks.length, 2);
  assert.deepEqual(view.sprites.map(row => row.displayType).sort(), ['actor', 'role']);
  assert.equal(view.sprites.find(row => row.displayType === 'actor').taskId,
    'task:codex-local:session-1:code');
  assert.equal(view.roles[0].taskId, 'task:codex-local:session-1:reason');
  assert.equal(view.roles[0].telemetry.model.value, 'model-a');
});

test('generic host Codex execution is shown as execution until a detailed action is known', () => {
  const canonical = snapshot({project, now, hostObservedAt: '2026-10-15T01:10:20Z',
    hostExecutions: [hostExecution()]});
  const row = scene(canonical).sprites[0];
  assert.equal(row.ownerId, 'entity:personal-ops-agent:host-codex');
  assert.equal(row.taskId, `task:personal-ops-agent:host-codex:${'a'.repeat(32)}`);
  assert.equal(row.displayType, 'actor');
  assert.equal(row.room, 'execution');
  assert.equal(row.action, 'execute');
});

test('a completed Codex role returns to waiting with the same identity used while working', () => {
  const success = projected([record({state: 'succeeded', action: 'reason'})]);
  const running = projected([record({action: 'reason'})]);
  assert.equal(success.sprites.length, 1);
  assert.equal(running.sprites.length, 1);
  assert.equal(success.sprites[0].id, running.sprites[0].id);
  assert.equal(success.sprites[0].id, 'role:analysis:generic:entity%3Acodex-local%3Asession-1');
  assert.equal(success.sprites[0].state, 'idle');
  assert.equal(success.sprites[0].room, 'waiting');
  assert.equal(success.sprites[0].invocationId, 'task:codex-local:session-1:task-1');
  assert.equal(success.sprites[0].invokedAt, '2026-10-15T01:10:10.000Z');
});

test('latest failure and meaningful unknown work remain in attention', () => {
  const failure = projected([record({state: 'failed', action: 'review'})]);
  assert.equal(failure.roles[0].state, 'failed');
  assert.equal(failure.roles[0].room, 'attention');

  const unknownCall = projected([record({actor_kind: 'mcp', source_kind: 'mcp', task_kind: 'call',
    action: 'read', state: 'unknown'})]);
  assert.equal(unknownCall.sprites[0].displayType, 'actor');
  assert.equal(unknownCall.sprites[0].room, 'attention');

  const unknownHost = scene(snapshot({project, now, hostExecutions: [hostExecution()]}));
  assert.equal(unknownHost.sprites[0].room, 'attention');
});

test('newer activity suppresses old failures and generic old session heartbeats stay off map', () => {
  const newer = projected([
    record({task_id: 'old', state: 'failed', action: 'review', time: '2026-10-15T01:09:00Z'}),
    record({task_id: 'new', action: 'code', time: '2026-10-15T01:10:20Z'}),
  ]);
  assert.equal(newer.roles.length, 0);
  assert.equal(newer.sprites.length, 1);
  assert.equal(newer.sprites[0].taskId, 'task:codex-local:session-1:new');

  const heartbeat = projected([record({state: 'unknown', action: 'execute', model: undefined})]);
  assert.deepEqual(heartbeat, {sprites: [], roles: []});
});

test('MCP owners get only scoped search roles and retain their real ownership and parent links', () => {
  const web = projected([record({source_id: 'search-mcp', source_kind: 'mcp', actor_id: 'search',
    actor_kind: 'mcp', task_kind: 'call', action: 'search', search_scope: 'web',
    parent_source_id: 'chat', parent_actor_id: 'agent', parent_task_id: 'turn'})]);
  assert.equal(web.sprites.length, 1);
  assert.equal(web.roles[0].ownerId, 'entity:search-mcp:search');
  assert.equal(web.roles[0].parentTaskId, 'task:chat:agent:turn');

  for (const search_scope of [undefined, 'files']) {
    const canonical = projected([record({source_id: 'search-mcp', source_kind: 'mcp', actor_id: 'search',
      actor_kind: 'mcp', task_kind: 'call', action: 'search', search_scope})]);
    assert.equal(canonical.roles.length, 0);
    assert.equal(canonical.sprites[0].displayType, 'actor');
  }
});

test('completed non-agent tools remain canonical without becoming role work', () => {
  const canonical = projected([record({source_id: 'repo-mcp', source_kind: 'mcp', actor_id: 'repo',
    actor_kind: 'mcp', task_kind: 'call', action: 'read', state: 'returned'})]);
  assert.equal(canonical.roles.length, 0);
  assert.equal(canonical.sprites.length, 1);
  assert.equal(canonical.sprites[0].id, 'entity:repo-mcp:repo');
  assert.equal(canonical.sprites[0].displayType, 'actor');
  assert.equal(canonical.sprites[0].taskId, 'task:repo-mcp:repo:task-1');
  assert.equal(canonical.sprites[0].state, 'idle');
});

test('pruned registration without explicit invocation evidence creates no sprites', () => {
  const objects = [
    {project, id: 'entity:known:repo', sourceId: 'known', sourceKind: 'mcp', localId: 'repo',
      kind: 'mcp', label: 'Repository MCP'},
    {project, id: 'entity:known:runner', sourceId: 'known', sourceKind: 'mcp', localId: 'runner',
      kind: 'executor', label: 'Command runner'},
    {project, id: 'entity:known:agent', sourceId: 'known', sourceKind: 'codex-cli', localId: 'agent',
      kind: 'agent', label: 'Old Codex'},
  ];
  const canonical = snapshot({project, now, objects});
  assert.equal(canonical.tasks.length, 0);
  assert.equal(canonical.actors.length, 3);

  const view = scene(canonical);
  assert.deepEqual(view.roles, []);
  assert.deepEqual(view.sprites, []);
});

test('concurrent work of the same role never leaks into a duplicate canonical body', () => {
  const view = projected([
    record({task_id: 'reason-a', action: 'reason', time: '2026-10-15T01:10:20Z'}),
    record({task_id: 'reason-b', action: 'reason', time: '2026-10-15T01:10:10Z'}),
  ]);
  assert.equal(view.roles.length, 1);
  assert.equal(view.sprites.length, 1);
  assert.equal(view.sprites[0].displayType, 'role');
});

test('a newer completed role call updates recency without hiding older active role work', () => {
  const view = projected([
    record({task_id: 'active-a', action: 'reason', state: 'running',
      time: '2026-10-15T01:10:10Z', model: 'model-a'}),
    record({task_id: 'quick-b', action: 'reason', state: 'running',
      time: '2026-10-15T01:10:15Z', model: 'model-b'}),
    record({task_id: 'quick-b', action: 'reason', state: 'succeeded',
      time: '2026-10-15T01:10:20Z', model: 'model-b'}),
  ]);

  assert.equal(view.sprites.length, 1);
  assert.equal(view.roles[0].taskId, 'task:codex-local:session-1:active-a');
  assert.equal(view.roles[0].state, 'running');
  assert.equal(view.roles[0].action, 'reason');
  assert.equal(view.roles[0].telemetry.model.value, 'model-a');
  assert.equal(view.roles[0].invocationId, 'task:codex-local:session-1:quick-b');
  assert.equal(view.roles[0].invokedAt, '2026-10-15T01:10:15.000Z');
});

test('caller observation order breaks equal-time invocation ties for an active role', () => {
  const canonical = snapshot({project, now, records: [
    record({task_id: 'a-old', action: 'reason', state: 'running',
      time: '2026-10-15T01:10:15Z', model: 'model-a'}),
    record({task_id: 'z-new', action: 'reason', state: 'running',
      time: '2026-10-15T01:10:15Z', model: 'model-b'}),
    record({task_id: 'z-new', action: 'reason', state: 'succeeded',
      time: '2026-10-15T01:10:20Z', model: 'model-b'}),
  ]});
  const view = scene(canonical, {invocationOrder: new Map([
    ['task:codex-local:session-1:a-old', 1],
    ['task:codex-local:session-1:z-new', 2],
  ])});

  assert.equal(view.roles[0].taskId, 'task:codex-local:session-1:a-old');
  assert.equal(view.roles[0].state, 'running');
  assert.equal(view.roles[0].telemetry.model.value, 'model-a');
  assert.equal(view.roles[0].invocationId, 'task:codex-local:session-1:z-new');
  assert.equal(view.roles[0].invokedAt, '2026-10-15T01:10:15.000Z');
});
