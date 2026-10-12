'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {snapshot} = require('../model.js');

const project = 'tossApp';
const now = Date.parse('2026-10-12T01:00:30Z');
const mcpId = 'entity:personal-ops-tunnel:coding-mcp';
const runnerId = 'entity:personal-ops-tunnel:commands';
const event = (call_id, tool, phase, time = '2026-10-12T01:00:00Z') => ({project, call_id, tool, phase, time});
const job = (overrides = {}) => ({project, job_id: 'a'.repeat(32), state: 'running', preset: 'web:build',
  label: '빌드', created_at: '2026-10-12T01:00:00Z', started_at: '2026-10-12T01:00:01Z',
  finished_at: null, exit_code: null, ...overrides});
const record = (overrides = {}) => ({schema_version: 1, project, source_id: 'codex-local', source_kind: 'codex-cli',
  actor_id: 'session-1', actor_kind: 'agent', actor_label: 'Codex', task_id: 'task-1', task_kind: 'agent',
  task_label: '파일 수정', action: 'code', state: 'running', time: '2026-10-12T01:00:10Z', ...overrides});

test('repeated tool calls are work items belonging to one persistent MCP object', () => {
  const result = snapshot({project, now, events: [event('first', 'replace_content', 'completed'),
    event('second', 'replace_content', 'completed'), event('third', 'read_file', 'started')]});
  assert.equal(result.actors.length, 1);
  assert.equal(result.actors[0].id, mcpId);
  assert.equal(result.actors[0].label, '코딩 MCP');
  assert.equal(result.actors[0].taskId, 'call:third');
  assert.equal(result.actors[0].action, 'read');
  assert.equal(result.tasks.length, 3);
  assert.ok(result.tasks.every(task => task.actorId === mcpId));
});

test('completed work goes to the archive while its same object returns to idle', () => {
  const running = snapshot({project, now, jobs: [job()]});
  const ended = snapshot({project, now, jobs: [job({state: 'succeeded', exit_code: 0, finished_at: '2026-10-12T01:00:20Z'})]});
  assert.equal(running.actors[0].id, ended.actors[0].id);
  assert.equal(running.actors[0].id, runnerId);
  assert.equal(running.actors[0].action, 'build');
  assert.equal(ended.actors[0].state, 'idle');
  assert.equal(ended.actors[0].room, 'waiting');
  assert.equal(ended.actors[0].taskId, null);
  assert.equal(ended.tasks[0].room, 'done');
  assert.equal(ended.counts.done, 1);
});

test('a returned execution request cannot finish a running command', () => {
  const result = snapshot({project, now, events: [event('launch', 'start_command', 'completed')], jobs: [job({preset: 'server:test'})]});
  assert.equal(result.actors.length, 2);
  assert.equal(result.actors.find(actor => actor.id === mcpId).state, 'idle');
  assert.equal(result.actors.find(actor => actor.id === runnerId).state, 'running');
  assert.equal(result.actors.find(actor => actor.id === runnerId).action, 'test');
  assert.deepEqual(result.counts, {active: 1, done: 1, attention: 0, unknown: 0, total: 2});
});

test('historical failure belongs to the work item instead of breaking the executor object', () => {
  const result = snapshot({project, now, jobs: [job({state: 'failed', exit_code: 1, finished_at: '2026-10-12T01:00:20Z'})]});
  assert.equal(result.actors[0].state, 'idle');
  assert.equal(result.tasks[0].room, 'attention');
  assert.equal(result.tasks[0].state, 'failed');
});

test('a common versioned format accepts explicit Codex and Claude identities without merging equal names', () => {
  const result = snapshot({project, now, records: [record(), record({source_id: 'claude-local', source_kind: 'claude-cli'})]});
  assert.deepEqual(result.actors.map(actor => actor.id).sort(), ['entity:claude-local:session-1', 'entity:codex-local:session-1']);
  assert.equal(new Set(result.actors.map(actor => actor.label)).size, 2);
  assert.equal(result.counts.active, 2);
});

test('canonical task lifecycle stays attached to the same actor and validates job success', () => {
  const result = snapshot({project, now, records: [record(), record({state: 'succeeded', time: '2026-10-12T01:00:20Z'}),
    record({task_id: 'job-1', task_kind: 'job', state: 'succeeded', exit_code: 9})]});
  assert.equal(result.actors.length, 1);
  assert.equal(result.tasks.length, 2);
  assert.equal(result.counts.done, 1);
  assert.equal(result.counts.unknown, 1);
  assert.equal(result.actors[0].state, 'unknown');
});

test('invalid or other-project records cannot invent agents and raw secrets never escape normalization', () => {
  const result = snapshot({project, now, records: [record({schema_version: 2}), record({actor_id: null}),
    record({source_kind: 'hidden-reasoning'}), record({project: 'another'}),
    record({argv: ['secret-token'], environment: {SECRET: 'secret-token'}})]});
  assert.equal(result.actors.length, 1);
  assert.equal(result.tasks.length, 1);
  assert.equal(JSON.stringify(result).includes('secret-token'), false);
});

test('one object retains all simultaneous work without duplicating its character', () => {
  const result = snapshot({project, now, jobs: [job(), job({job_id: 'b'.repeat(32), preset: 'web:test'})]});
  assert.equal(result.actors.length, 1);
  assert.equal(result.actors[0].activeCount, 2);
  assert.equal(result.tasks.filter(task => task.actorId === runnerId).length, 2);
});

test('namespace boundaries cannot collide and normalization does not mutate input', () => {
  const records = [record({source_id: 'a:b', actor_id: 'c'}), record({source_id: 'a', actor_id: 'b:c'})];
  const before = JSON.stringify(records);
  const result = snapshot({project, now, records});
  assert.equal(new Set(result.actors.map(actor => actor.id)).size, 2);
  assert.equal(JSON.stringify(records), before);
});

test('tasks from different agents stay separate even when their local task IDs match', () => {
  const result = snapshot({project, now, records: [record(), record({actor_id: 'session-2'})]});
  assert.equal(result.tasks.length, 2);
  assert.equal(result.actors.length, 2);
});

test('running work takes priority over newer queued work on the same executor', () => {
  const result = snapshot({project, now, jobs: [job(), job({job_id: 'b'.repeat(32), state: 'queued',
    created_at: '2026-10-12T01:00:20Z', started_at: null})]});
  assert.equal(result.actors[0].state, 'running');
  assert.equal(result.actors[0].taskId, 'job:' + 'a'.repeat(32));
});

test('known objects survive a pruned recent window without inventing work or liveness', () => {
  const first = snapshot({project, now, events: [event('first', 'read_file', 'completed')]});
  const pruned = snapshot({project, now, objects: first.actors});
  assert.equal(pruned.actors.length, 1);
  assert.equal(pruned.actors[0].id, mcpId);
  assert.equal(pruned.actors[0].state, 'unobserved');
  assert.equal(pruned.actors[0].taskId, null);
  assert.equal(pruned.tasks.length, 0);
  assert.equal(pruned.counts.active, 0);
  assert.equal(snapshot({project: 'another', objects: first.actors, now}).actors.length, 0);
});

test('a failure is not replaced by success when canonical event timestamps tie', () => {
  for (const states of [['failed', 'succeeded'], ['succeeded', 'failed']]) {
    const result = snapshot({project, now, records: states.map(state => record({state}))});
    assert.equal(result.tasks.length, 1);
    assert.equal(result.tasks[0].state, 'failed');
    assert.equal(result.counts.attention, 1);
    assert.equal(result.counts.done, 0);
  }
});

test('incompatible completion states cannot invent successful calls or commands', () => {
  const result = snapshot({project, now, records: [record({task_kind:'call', state:'succeeded'}),
    record({task_id:'job-2', task_kind:'job', state:'returned'}),
    record({task_id:'agent-2', task_kind:'agent', state:'returned'})]});
  assert.equal(result.tasks.length, 0);
  assert.equal(result.counts.done, 0);
});

test('malformed structured IDs and actions cannot abort other valid records', () => {
  const malformed = {toString:null, valueOf:null};
  const result = snapshot({project, now, jobs:[job({job_id:malformed})],
    records:[record({action:malformed}), record()]});
  assert.equal(result.tasks.length, 1);
  assert.equal(result.tasks[0].action, 'code');
});

test('safe action metadata identifies a direct build even when its job label is generic', () => {
  const result = snapshot({project, now, jobs:[job({preset:null,label:'직접 명령 실행',action:'build'})]});
  assert.equal(result.tasks[0].action, 'build');
  assert.equal(result.tasks[0].sourceRoom, 'testing');
  assert.equal(result.actors[0].action, 'build');
});
