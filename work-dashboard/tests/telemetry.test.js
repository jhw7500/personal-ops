'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {snapshot} = require('../model.js');
const project = 'tossApp';
const now = Date.parse('2026-10-12T01:00:30Z');
const event = (overrides = {}) => ({schema_version: 1, project, source_id: 'chat-session',
  source_kind: 'chatgpt', actor_id: 'agent-1', actor_kind: 'agent', actor_label: 'ChatGPT',
  task_id: 'turn-1', task_kind: 'agent', task_label: '요구사항 검토', action: 'reason',
  state: 'running', time: '2026-10-12T01:00:10Z', provenance: 'reported', ...overrides});

test('explicit reasoning events show one agent and supplied model metadata', () => {
  const result = snapshot({project, now, records: [event({model: 'fixture-model', reasoning_effort: 'high',
    usage: {input_tokens: 25, output_tokens: 0, reasoning_tokens: 0}, usage_scope: 'request'})]});
  assert.equal(result.actors.length, 1);
  const actor = result.actors[0];
  assert.equal(actor.action, 'reason');
  assert.equal(actor.room, 'analysis');
  assert.equal(actor.telemetry.model.value, 'fixture-model');
  assert.equal(actor.telemetry.reasoningEffort.value, 'high');
  assert.deepEqual(actor.telemetry.usage.value, {input_tokens: 25, output_tokens: 0, reasoning_tokens: 0});
  assert.equal(actor.telemetry.usage.scope, 'request');
  assert.equal(actor.telemetry.usage.provenance, 'reported');
  assert.equal(Object.hasOwn(actor.telemetry.usage.value, 'total_tokens'), false);
});

test('separate MCP objects retain explicit cross-source parent relationships', () => {
  const result = snapshot({project, now, records: [event(), event({source_id: 'figma-cloud', source_kind: 'mcp',
    actor_id: 'figma', actor_kind: 'mcp', actor_label: 'Figma', task_id: 'fetch-1', task_kind: 'call',
    action: 'read', parent_source_id: 'chat-session', parent_actor_id: 'agent-1', parent_task_id: 'turn-1'})]});
  assert.equal(result.actors.length, 2);
  const mcp = result.tasks.find(row => row.sourceId === 'figma-cloud');
  assert.equal(mcp.parentTaskId, 'task:chat-session:agent-1:turn-1');
  assert.equal(mcp.provenance, 'reported');
  assert.equal(result.actors.find(row => row.kind === 'mcp').label, 'Figma');
});

test('partial lifecycle events preserve supplied metadata without creating another model character', () => {
  const records = [event({model: 'fixture-model', reasoning_effort: 'high'}),
    event({state: 'succeeded', time: '2026-10-12T01:00:20Z'})];
  const result = snapshot({project, now, records});
  assert.equal(result.actors.length, 1);
  assert.equal(result.actors[0].state, 'idle');
  assert.equal(result.actors[0].telemetry.model.value, 'fixture-model');
  assert.equal(result.tasks[0].telemetry.reasoningEffort.time, '2026-10-12T01:00:10.000Z');
});

test('a model change clears old effort and token counts, including across tasks', () => {
  const result = snapshot({project, now, records: [event({model: 'old-model', reasoning_effort: 'high',
    usage: {total_tokens: 99}, usage_scope: 'session'}),
  event({task_id: 'turn-2', model: 'new-model', time: '2026-10-12T01:00:20Z'})]});
  assert.equal(result.actors.length, 1);
  assert.equal(result.actors[0].telemetry.model.value, 'new-model');
  assert.equal(Object.hasOwn(result.actors[0].telemetry, 'usage'), false);
  assert.equal(Object.hasOwn(result.actors[0].telemetry, 'reasoningEffort'), false);
});

test('all incomplete canonical work expires instead of animating forever', () => {
  for (const state of ['started', 'running', 'queued', 'canceling']) {
    const result = snapshot({project, now: now + 180000, records: [event({state})]});
    assert.equal(result.actors[0].state, 'unknown');
    assert.equal(result.counts.active, 0);
    assert.equal(result.actors[0].room, 'waiting');
  }
  const early = snapshot({project, now, records: [event({expires_at: '2026-10-12T01:00:15Z'})]});
  assert.equal(early.actors[0].state, 'unknown');
});

test('missing usage, invalid metadata and reasoning configuration do not invent values or actions', () => {
  const result = snapshot({project, now, records: [event({action: 'execute', model: '<bad>',
    reasoning_effort: {level: 'high'}, usage: {input_tokens: true, total_tokens: -1}, usage_scope: 'unknown',
    thinking: {enabled: true}, arguments: 'private-sentinel', reasoning_text: 'private-sentinel'})]});
  assert.equal(result.actors[0].action, 'execute');
  assert.deepEqual(result.actors[0].telemetry, {});
  assert.equal(JSON.stringify(result).includes('private-sentinel'), false);
});

test('model and usage keep their own evidence provenance and do not sum request counts', () => {
  const result = snapshot({project, now, records: [event({model: 'fixture-model'}),
    event({time: '2026-10-12T01:00:20Z', provenance: 'observed', usage: {total_tokens: 5}, usage_scope: 'request'}),
    event({task_id: 'turn-2', time: '2026-10-12T01:00:25Z', provenance: 'observed',
      usage: {total_tokens: 7}, usage_scope: 'request'})]});
  const telemetry = result.actors[0].telemetry;
  assert.equal(telemetry.model.provenance, 'reported');
  assert.equal(telemetry.usage.provenance, 'observed');
  assert.equal(telemetry.usage.value.total_tokens, 7);
});

test('same-time metadata uses receipt time and newest-first persistence order across task boundaries', () => {
  for (const task_id of ['turn-1', 'turn-2']) {
    const older = event({model: 'old-model', reasoning_effort: 'high', usage: {total_tokens: 99}, usage_scope: 'request',
      received_at: '2026-10-12T01:00:11Z'});
    const newer = event({task_id, model: 'new-model', received_at: '2026-10-12T01:00:12Z'});
    for (const records of [[newer, older], [older, newer]]) {
      const telemetry = snapshot({project, now, records}).actors[0].telemetry;
      assert.equal(telemetry.model.value, 'new-model');
      assert.equal(Object.hasOwn(telemetry, 'usage'), false);
      assert.equal(Object.hasOwn(telemetry, 'reasoningEffort'), false);
    }
    // Same receipt stamp within a batch: the API returns the last persisted row first.
    const tied = {...newer, received_at: older.received_at};
    const telemetry = snapshot({project, now, records: [tied, older]}).actors[0].telemetry;
    assert.equal(telemetry.model.value, 'new-model');
    assert.equal(Object.hasOwn(telemetry, 'usage'), false);
  }
});
