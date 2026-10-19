'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {snapshot, speechEvents} = require('../model.js');

const PROJECT = 'tossApp';
const NOW = Date.parse('2026-10-15T03:00:30Z');

function row(taskId, state, time, overrides = {}) {
  return {
    schema_version: 1,
    project: PROJECT,
    source_id: 'speech-source',
    source_kind: 'codex-cli',
    actor_id: 'speech-agent',
    actor_kind: 'agent',
    actor_label: 'Speech agent',
    task_id: taskId,
    task_kind: 'agent',
    task_label: `Task ${taskId}`,
    action: 'code',
    state,
    time,
    provenance: 'observed',
    ...overrides,
  };
}

function canonical(records, project = PROJECT, now = NOW) {
  return snapshot({project, records, now});
}

function transition(taskId, beforeState, afterState, overrides = {}) {
  const start = row(taskId, beforeState, '2026-10-15T03:00:00Z');
  const previous = canonical([start]);
  const current = canonical([start, row(taskId, afterState, '2026-10-15T03:00:20Z', overrides)]);
  return {previous, current};
}

test('speechEvents emits one factual start bubble only when an invoked task becomes active', () => {
  const waiting = canonical([row('start', 'queued', '2026-10-15T03:00:00Z')]);
  const current = canonical([
    row('start', 'started', '2026-10-15T03:00:10Z'),
    row('queued', 'queued', '2026-10-15T03:00:11Z'),
  ]);

  const events = speechEvents(waiting, current, {now: NOW});

  assert.equal(events.length, 1);
  assert.equal(events[0].id, 'task:speech-source:speech-agent:start');
  assert.deepEqual(events[0].speech,
    {kind: 'start', tone: 'info', text: '작업 시작!', priority: 1, durationMs: 4000});
  assert.equal(events[0].provenance, 'observed');
  assert.equal(events[0].invokedAt, '2026-10-15T03:00:10.000Z');
});

test('speechEvents does not repeat on heartbeats or a started-to-running state update with the same action', () => {
  const started = canonical([row('same', 'started', '2026-10-15T03:00:10Z')]);
  const running = canonical([
    row('same', 'started', '2026-10-15T03:00:10Z'),
    row('same', 'running', '2026-10-15T03:00:20Z'),
  ]);
  const heartbeat = canonical([
    row('same', 'started', '2026-10-15T03:00:10Z'),
    row('same', 'running', '2026-10-15T03:00:20Z', {received_at: '2026-10-15T03:00:25Z'}),
  ]);

  assert.deepEqual(speechEvents(started, running, {now: NOW}), []);
  assert.deepEqual(speechEvents(running, heartbeat, {now: NOW}), []);
});

test('speechEvents emits a brief factual action bubble only for an actual active action change', () => {
  const previous = canonical([row('action', 'running', '2026-10-15T03:00:05Z', {action: 'code'})]);
  const current = canonical([
    row('action', 'running', '2026-10-15T03:00:05Z', {action: 'code'}),
    row('action', 'running', '2026-10-15T03:00:20Z', {action: 'test'}),
  ]);

  const [event] = speechEvents(previous, current, {now: NOW});

  assert.deepEqual(event.speech,
    {kind: 'action', tone: 'info', text: '테스트 중!', priority: 1, durationMs: 4000});
  assert.equal(event.action, 'test');
});

test('speechEvents keeps an unknown host action generic', () => {
  const operation = 'a'.repeat(32);
  const previous = canonical([row(operation, 'running', '2026-10-15T03:00:05Z', {
    source_id: 'personal-ops-host', source_kind: 'mcp', actor_id: 'host-executor',
    actor_kind: 'executor', task_kind: 'job', action: 'code',
  })]);
  const current = snapshot({project: PROJECT, now: NOW, hostObservedAt: '2026-10-15T03:00:25Z',
    hostExecutions: [{
      project: PROJECT,
      operation_id: operation,
      kind: 'command',
      state: 'running',
      created_at: '2026-10-15T03:00:10Z',
      started_at: '2026-10-15T03:00:20Z',
      finished_at: null,
      result: null,
      termination_verified: false,
      display_view: true,
    }],
  });

  const [event] = speechEvents(previous, current, {now: NOW});

  assert.equal(event.speech.kind, 'action');
  assert.equal(event.speech.text, '작업 진행 중!');
  assert.equal(event.speech.text.includes('실행'), false);
});

test('speechEvents distinguishes every terminal outcome without inventing success or failure', () => {
  const cases = [
    ['succeeded', {}, {kind: 'complete', tone: 'success', text: '작업 끝!', priority: 2, durationMs: 4000}],
    ['returned', {task_kind: 'call'}, {kind: 'returned', tone: 'info', text: '응답 보냈어요', priority: 1, durationMs: 4000}],
    ['failed', {}, {kind: 'failed', tone: 'danger', text: '앗, 문제가 생겼어요!', priority: 3, durationMs: 6000}],
    ['timed_out', {}, {kind: 'timed_out', tone: 'warning', text: '제한 시간을 넘겼어요', priority: 2, durationMs: 6000}],
    ['canceled', {}, {kind: 'canceled', tone: 'neutral', text: '작업이 취소됐어요', priority: 2, durationMs: 6000}],
    ['interrupted', {}, {kind: 'interrupted', tone: 'warning', text: '작업이 중단됐어요', priority: 2, durationMs: 6000}],
    ['unknown', {}, {kind: 'unknown', tone: 'warning', text: '결과 확인이 필요해요', priority: 2, durationMs: 6000}],
  ];

  for (const [state, overrides, expected] of cases) {
    const {previous, current} = transition(`terminal-${state}`, 'running', state, overrides);
    const events = speechEvents(previous, current, {now: NOW});
    assert.equal(events.length, 1, state);
    assert.deepEqual(events[0].speech, expected, state);
  }
});

test('speechEvents permits fresh verified success after an unknown result but never repeats a terminal state', () => {
  const previous = canonical([row('eventual', 'unknown', '2026-10-15T03:00:05Z')]);
  const succeeded = canonical([
    row('eventual', 'unknown', '2026-10-15T03:00:05Z'),
    row('eventual', 'succeeded', '2026-10-15T03:00:20Z', {exit_code: 0}),
  ]);
  const metadataOnly = canonical([
    row('eventual', 'unknown', '2026-10-15T03:00:05Z'),
    row('eventual', 'succeeded', '2026-10-15T03:00:20Z', {exit_code: 0, received_at: '2026-10-15T03:00:25Z'}),
  ]);

  assert.equal(speechEvents(previous, succeeded, {now: NOW})[0].speech.kind, 'complete');
  assert.deepEqual(speechEvents(succeeded, metadataOnly, {now: NOW}), []);
});

test('speechEvents rejects an older transition for the same task while allowing a forward completion', () => {
  for (const [state, overrides] of [['failed', {}], ['returned', {task_kind: 'call'}]]) {
    const previous = canonical([row(`regressed-${state}`, 'running', '2026-10-15T03:00:20Z')]);
    const regressed = canonical([row(`regressed-${state}`, state, '2026-10-15T03:00:10Z', overrides)]);

    assert.deepEqual(speechEvents(previous, regressed, {now: NOW}), [], state);
  }

  const previous = canonical([row('forward', 'running', '2026-10-15T03:00:10Z')]);
  const completed = canonical([
    row('forward', 'running', '2026-10-15T03:00:10Z'),
    row('forward', 'succeeded', '2026-10-15T03:00:20Z', {exit_code: 0}),
  ]);
  assert.equal(speechEvents(previous, completed, {now: NOW})[0].speech.kind, 'complete');
});

test('speechEvents fails closed for initial history, cross-project data, old additions, invalid now, and non-invoked tasks', () => {
  const old = canonical([row('old', 'failed', '2026-10-15T02:59:00Z')]);
  const empty = canonical([]);
  const other = canonical([], 'other');
  const nonInvoked = canonical([row('waiting', 'queued', '2026-10-15T03:00:20Z')]);

  assert.deepEqual(speechEvents(null, old, {now: NOW}), []);
  assert.deepEqual(speechEvents(empty, other, {now: NOW}), []);
  assert.deepEqual(speechEvents(empty, old, {now: NOW}), []);
  assert.deepEqual(speechEvents(empty, nonInvoked, {now: NOW}), []);
  assert.deepEqual(speechEvents(empty, nonInvoked, {now: Number.NaN}), []);
});

test('speechEvents returns canonical copies with provenance and validated numeric results unchanged', () => {
  const previous = canonical([row('safe', 'running', '2026-10-15T03:00:00Z')]);
  const current = canonical([
    row('safe', 'running', '2026-10-15T03:00:00Z'),
    row('safe', 'succeeded', '2026-10-15T03:00:20Z', {
      provenance: 'reported', exit_code: 0, stdout: 'private', args: ['private'],
    }),
  ]);
  const sourceTask = current.tasks[0];

  const [event] = speechEvents(previous, current, {now: NOW});

  assert.notEqual(event, sourceTask);
  assert.equal(event.provenance, 'reported');
  assert.deepEqual(event.result, {exit_code: 0});
  assert.equal(JSON.stringify(event).includes('private'), false);
  assert.equal(Object.hasOwn(sourceTask, 'speech'), false);
});
