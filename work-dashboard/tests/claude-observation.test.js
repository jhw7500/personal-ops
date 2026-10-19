'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {scene, snapshot} = require('../model.js');

const project = 'wlan-package';
const now = Date.parse('2026-10-19T07:30:00Z');
const stamp = seconds => new Date(now - seconds * 1000).toISOString();
const row = (overrides = {}) => ({schema_version: 1, project, source_id: 'claude-local',
  source_kind: 'claude-cli', actor_id: 'session-1', actor_kind: 'agent', actor_label: 'Claude',
  task_id: 'prompt:request-1', task_kind: 'call', task_label: 'Claude prompt',
  action: 'execute', state: 'started', time: stamp(300), provenance: 'observed', ...overrides});
const tool = (overrides = {}) => row({task_id: 'tool:recent', task_label: 'Bash', tool_name: 'Bash',
  state: 'returned', time: stamp(1), ...overrides});
const view = records => snapshot({project, now, records});

test('expired Claude request stays visibly uncertain beside a completed tool without changing idle replay posture', () => {
  const result = view([row(), tool({state: 'started', time: stamp(2)}), tool()]);
  const actor = result.actors[0];
  assert.deepEqual(actor.observationStatus, {kind: 'prompt', taskId: 'task:claude-local:session-1:prompt%3Arequest-1',
    time: stamp(300)});
  assert.equal(result.tasks.find(task => task.label === 'Claude prompt').state, 'unknown');
  assert.equal(result.counts.active, 0);
  const sprite = scene(result).sprites[0];
  assert.equal(sprite.state, 'idle');
  assert.equal(sprite.room, 'waiting');
  assert.deepEqual(sprite.observationStatus, actor.observationStatus);
  assert.equal(result.tasks.find(task => task.tool === 'Bash').replayable, true);
});

test('matching prompt result or newer completed prompt clears the expired request observation', () => {
  for (const records of [
    [row(), row({state: 'returned', time: stamp(3)}), tool()],
    [row(), row({task_id: 'prompt:new-request', time: stamp(20)}),
      row({task_id: 'prompt:new-request', state: 'returned', time: stamp(10)}), tool()],
  ]) assert.equal(view(records).actors[0].observationStatus, undefined);
});

test('a late result for an older prompt cannot hide the newer expired prompt with a retained start', () => {
  const records = [row({task_id: 'prompt:older', time: stamp(600)}),
    row({task_id: 'prompt:older', state: 'returned', time: stamp(1)}), row(), tool()];
  assert.equal(view(records).actors[0].observationStatus?.taskId,
    'task:claude-local:session-1:prompt%3Arequest-1');
});

test('a terminal-only prompt without a retained start cannot supersede a different unresolved invocation', () => {
  for (const provenance of ['observed', 'reported']) {
    const records = [row(), row({task_id: 'prompt:older-unanchored', state: 'returned', time: stamp(1)}),
      ...(provenance === 'reported' ? [row({task_id: 'prompt:older-unanchored',
        time: stamp(200), provenance})] : []), tool()];
    assert.equal(view(records).actors[0].observationStatus?.taskId,
      'task:claude-local:session-1:prompt%3Arequest-1');
  }
});

test('expired subagent lifecycle stays with the exact child while its new tool is observed separately', () => {
  const child = {actor_id: 'sub:session-1:child-1', actor_label: 'general-purpose'};
  const start = row({...child, task_id: 'agent:session-1:child-1', task_label: 'Claude subagent'});
  const result = view([start, tool({...child, state: 'running'}), tool({actor_id: 'session-1'})]);
  const actor = result.actors.find(item => item.localId === child.actor_id);
  assert.deepEqual(actor.observationStatus, {kind: 'subagent',
    taskId: 'task:claude-local:sub%3Asession-1%3Achild-1:agent%3Asession-1%3Achild-1', time: stamp(300)});
  assert.equal(scene(result).sprites.find(item => item.ownerId === actor.id).state, 'running');
  assert.equal(result.actors.find(item => item.localId === 'session-1').observationStatus, undefined);
  assert.equal(result.counts.active, 1);
  const returned = row({...child, task_id: start.task_id, state: 'returned', time: stamp(1)});
  assert.equal(view([start, returned]).actors[0].observationStatus, undefined);
  assert.equal(view([returned]).actors[0].observationStatus, undefined);
});

test('generic session metadata, reported lifecycle, tools and mismatched child identities do not acquire an observation badge', () => {
  const cases = [
    row({task_kind: 'agent', task_id: 'session:session-1', state: 'unknown'}),
    row({task_kind: 'agent', task_id: 'session:session-1', state: 'running'}),
    row({provenance: 'reported'}), row({source_kind: 'codex-cli'}),
    row({task_id: 'tool:old', task_label: 'Bash', tool_name: 'Bash'}),
    row({actor_id: 'sub:session-1:child-1', task_id: 'agent:session-2:child-1'}),
    row({actor_kind: 'mcp'}), row({action: 'code'}),
  ];
  for (const record of cases) assert.equal(view([record]).actors[0].observationStatus, undefined);
});

test('lifecycle evidence does not cross collector, session or project and disappears with pruned records', () => {
  const records = [row(), row({project: 'other'}),
    row({actor_id: 'session-2', task_id: 'prompt:request-1', state: 'returned', time: stamp(1)}),
    row({source_id: 'other-collector', state: 'returned', time: stamp(1)})];
  const result = view(records);
  assert.equal(result.actors.filter(actor => actor.observationStatus).length, 1);
  assert.equal(result.actors.find(actor => actor.observationStatus).id, 'entity:claude-local:session-1');
  const pruned = snapshot({project, now, objects: result.actors});
  assert.ok(pruned.actors.every(actor => actor.observationStatus === undefined));
  assert.equal(pruned.counts.active, 0);
});

test('equal prompt start stamps preserve uncertainty without inventing a current execution', () => {
  const records = [row(), row({task_id: 'prompt:other', time: stamp(300)}),
    row({task_id: 'prompt:other', state: 'returned', time: stamp(1)}), tool()];
  for (const list of [records, [...records].reverse()]) {
    const result = view(list);
    assert.equal(result.actors[0].observationStatus?.kind, 'prompt');
    assert.equal(result.counts.active, 0);
  }
});
