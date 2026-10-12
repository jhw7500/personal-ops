'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {snapshot, changes} = require('../model.js');
const project = 'alpha';
const now = Date.parse('2026-10-12T12:00:05Z');
const event = (overrides = {}) => ({schema_version: 1, project,
  source_id: 'codex-local', source_kind: 'codex-cli', actor_id: 'session-1', actor_kind: 'agent',
  actor_label: 'Codex', task_id: 'tool-1', task_kind: 'call', task_label: '파일 수정',
  action: 'code', state: 'started', time: '2026-10-12T12:00:01Z', provenance: 'observed', ...overrides});
const view = records => snapshot({project, records, now});

test('initial and cross-project snapshots never announce historical work as new', () => {
  const current = view([event()]);
  assert.deepEqual(changes(null, current, {now}), []);
  assert.deepEqual(changes({...view([]), project: 'beta'}, current, {now}), []);
});

test('a new start and supplied return each produce one real state change', () => {
  const empty = view([]), started = view([event()]);
  const returned = view([event({state: 'returned', time: '2026-10-12T12:00:04Z'})]);
  assert.equal(changes(empty, started, {now}).length, 1);
  const result = changes(started, returned, {now});
  assert.equal(result.length, 1);
  assert.equal(result[0].state, 'returned');
  assert.equal(result[0].room, 'done');
  assert.equal(result[0].sourceRoom, 'coding');
  assert.equal(result[0].provenance, 'observed');
  assert.equal(result[0].actorId, started.actors[0].id);
});

test('native MCP changes retain the tool identifier needed for readable titles', () => {
  const current = snapshot({project, now, events:[{project, tool:'git_status', call_id:'native-git',
    phase:'completed', time:'2026-10-12T12:00:04Z'}]});
  const result = changes(view([]), current, {now});
  assert.equal(result.length, 1);
  assert.equal(result[0].tool, 'git_status');
  assert.equal(result[0].state, 'returned');
});

test('timestamps, model metadata and heartbeat refreshes do not invent new activity', () => {
  const first = view([event({task_id: 'session-activity', task_kind: 'agent', state: 'running'})]);
  const heartbeat = view([event({task_id: 'session-activity', task_kind: 'agent', state: 'running',
    time: '2026-10-12T12:00:04Z', model: 'new-config', model_source: 'configuration'})]);
  assert.deepEqual(changes(first, heartbeat, {now}), []);
  assert.deepEqual(changes(first, first, {now}), []);
});

test('disappearing and stale catch-up work never create fake completions or live bursts', () => {
  const first = view([event()]);
  assert.deepEqual(changes(first, view([]), {now}), []);
  const old = view([event({time: '2026-10-12T11:50:00Z', state: 'returned'})]);
  assert.deepEqual(changes(view([]), old, {now}), []);
});

test('a sampled cancellation remains visible when the producer only has its older start timestamp', () => {
  const first = view([event({task_kind: 'job', state: 'running', time: '2026-10-12T11:59:30Z'})]);
  const canceled = view([event({task_kind: 'job', state: 'canceling', time: '2026-10-12T11:59:30Z'})]);
  const result = changes(first, canceled, {now});
  assert.equal(result.length, 1);
  assert.equal(result[0].state, 'canceling');
  assert.equal(result[0].time, '2026-10-12T11:59:30.000Z');
  assert.equal(result[0].noticedAt, '2026-10-12T12:00:05.000Z');
});

test('real action changes are announced and malformed events cannot leak private fields', () => {
  const first = view([event({task_kind: 'agent', state: 'running'})]);
  const current = view([event({task_kind: 'agent', state: 'running', action: 'read',
    time: '2026-10-12T12:00:04Z', arguments: 'private-sentinel'})]);
  const result = changes(first, current, {now});
  assert.equal(result.length, 1);
  assert.equal(result[0].action, 'read');
  assert.equal(JSON.stringify(result).includes('private-sentinel'), false);
});
