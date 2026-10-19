'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {recentTasks, resumeChanges, snapshot} = require('../model.js');

const PROJECT = 'tossApp';
const SINCE = Date.parse('2026-10-15T02:00:00Z');
const NOW = Date.parse('2026-10-15T02:10:00Z');

function record(taskId, state, time, overrides = {}) {
  return {
    schema_version: 1,
    project: PROJECT,
    source_id: 'history-source',
    source_kind: 'codex-cli',
    actor_id: 'history-agent',
    actor_kind: 'agent',
    actor_label: 'History agent',
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

function canonical(records, options = {}) {
  return snapshot({project: PROJECT, records, now: NOW, ...options});
}

test('recentTasks returns a newest-first copy across kinds and states', () => {
  const tasks = [
    {id: 'job-old', kind: 'job', state: 'succeeded', time: '2026-10-15T01:00:00Z'},
    {id: 'call-new', kind: 'call', state: 'returned', time: '2026-10-15T03:00:00Z'},
    {id: 'agent-mid', kind: 'agent', state: 'running', time: '2026-10-15T02:00:00Z'},
  ];
  const before = tasks.slice();

  assert.deepEqual(recentTasks(tasks).map(task => task.id), ['call-new', 'agent-mid', 'job-old']);
  assert.deepEqual(tasks, before);
});

test('history helpers are available through the browser global', () => {
  const fs = require('node:fs');
  const path = require('node:path');
  const vm = require('node:vm');
  const modelPath = path.join(__dirname, '..', 'model.js');
  const context = {window: {}};
  vm.runInNewContext(fs.readFileSync(modelPath, 'utf8'), context, {filename: modelPath});

  assert.equal(typeof context.window.WorkDashboardModel.recentTasks, 'function');
  assert.equal(typeof context.window.WorkDashboardModel.resumeChanges, 'function');
});

test('recentTasks resolves equal and invalid times with observation rank then deterministic ID', () => {
  const tasks = [
    {id: 'z-invalid', time: 'invalid'},
    {id: 'b', time: '2026-10-15T02:00:00Z'},
    {id: 'a', time: '2026-10-15T02:00:00Z'},
    {id: 'a-invalid', time: null},
    {id: 'c', time: '2026-10-15T02:00:00Z'},
  ];
  const order = new Map([['a', 2], ['b', 8], ['c', 'not-numeric']]);

  assert.deepEqual(recentTasks(tasks, {order}).map(task => task.id),
    ['b', 'a', 'c', 'a-invalid', 'z-invalid']);
});

test('resumeChanges finds interval history even when returning after more than 15 seconds', () => {
  const previous = canonical([
    record('running', 'running', '2026-10-15T01:59:00Z'),
    record('failed-before', 'failed', '2026-10-15T01:58:00Z'),
    record('done-before', 'succeeded', '2026-10-15T01:57:00Z'),
    record('missing', 'running', '2026-10-15T01:59:03Z'),
  ], {now: SINCE - 1});
  const current = canonical([
    record('running', 'succeeded', '2026-10-15T02:01:00Z', {exit_code: 0}),
    record('failed-before', 'timed_out', '2026-10-15T02:02:00Z'),
    record('done-before', 'returned', '2026-10-15T02:03:00Z', {task_kind: 'call'}),
    record('new-success', 'succeeded', '2026-10-15T02:04:00Z', {exit_code: 0}),
    record('new-failure', 'failed', '2026-10-15T02:05:00Z'),
    record('old-history', 'failed', '2026-10-15T01:50:00Z'),
  ]);

  const result = resumeChanges(previous, current, {since: SINCE, now: NOW});

  assert.deepEqual(result.newTasks.map(task => task.id), [
    'task:history-source:history-agent:new-success',
    'task:history-source:history-agent:new-failure',
  ]);
  assert.deepEqual(result.completed.map(task => task.id), [
    'task:history-source:history-agent:running',
    'task:history-source:history-agent:new-success',
  ]);
  assert.deepEqual(result.attention.map(task => task.id), [
    'task:history-source:history-agent:new-failure',
  ]);
  assert.deepEqual(result.changed.map(task => task.id), [
    'task:history-source:history-agent:running',
    'task:history-source:history-agent:failed-before',
    'task:history-source:history-agent:done-before',
  ]);
  assert.deepEqual(result.missing.map(task => task.id), [
    'task:history-source:history-agent:missing',
  ]);
  assert.equal(result.changed[0].label, 'Task running');
  assert.equal(result.changed[0].provenance, 'observed');
  assert.equal(result.changed[0].result.exit_code, 0);
});

test('resumeChanges treats missing and unknown as attention rather than completion', () => {
  const previous = canonical([
    record('vanished', 'running', '2026-10-15T01:59:00Z'),
    record('unknown', 'running', '2026-10-15T01:59:01Z'),
    record('already-failed', 'failed', '2026-10-15T01:59:02Z'),
  ], {now: SINCE - 1});
  const current = canonical([
    record('unknown', 'unknown', '2026-10-15T02:01:00Z'),
    record('already-failed', 'timed_out', '2026-10-15T02:02:00Z'),
  ]);

  const result = resumeChanges(previous, current, {since: SINCE, now: NOW});

  assert.deepEqual(result.completed, []);
  assert.deepEqual(result.attention.map(task => task.id), ['task:history-source:history-agent:unknown']);
  assert.deepEqual(result.missing.map(task => task.id), ['task:history-source:history-agent:vanished']);
});

test('resumeChanges counts fresh observed success and return transitions after unknown', () => {
  const previous = canonical([
    record('eventual-success', 'unknown', '2026-10-15T01:59:00Z'),
    record('eventual-return', 'unknown', '2026-10-15T01:59:01Z', {task_kind: 'call'}),
  ], {now: SINCE - 1});
  const current = canonical([
    record('eventual-success', 'succeeded', '2026-10-15T02:06:00Z', {exit_code: 0}),
    record('eventual-return', 'returned', '2026-10-15T02:07:00Z', {task_kind: 'call'}),
  ]);

  const result = resumeChanges(previous, current, {since: SINCE, now: NOW});

  assert.deepEqual(result.completed.map(task => [task.id, task.state, task.provenance]), [
    ['task:history-source:history-agent:eventual-success', 'succeeded', 'observed'],
    ['task:history-source:history-agent:eventual-return', 'returned', 'observed'],
  ]);
  assert.deepEqual(result.changed.map(task => task.id), [
    'task:history-source:history-agent:eventual-success',
    'task:history-source:history-agent:eventual-return',
  ]);
  assert.deepEqual(result.attention, []);
  assert.deepEqual(result.missing, []);
});

test('resumeChanges fails closed for invalid snapshots, bounds, and cross-project comparisons', () => {
  const empty = {newTasks: [], completed: [], attention: [], changed: [], missing: []};
  const valid = canonical([record('one', 'running', '2026-10-15T02:01:00Z')]);
  const other = snapshot({project: 'other', now: NOW, records: []});

  assert.deepEqual(resumeChanges(null, valid, {since: SINCE, now: NOW}), empty);
  assert.deepEqual(resumeChanges(valid, valid, {since: 'bad', now: NOW}), empty);
  assert.deepEqual(resumeChanges(valid, valid, {since: NOW, now: SINCE}), empty);
  assert.deepEqual(resumeChanges(valid, other, {since: SINCE, now: NOW}), empty);
});

test('snapshot exposes only validated numeric results from records and jobs', () => {
  const result = snapshot({project: PROJECT, now: NOW,
    records: [
      record('safe', 'succeeded', '2026-10-15T02:01:00Z', {exit_code: 0, stdout: 'secret'}),
      record('unsafe', 'failed', '2026-10-15T02:02:00Z', {exit_code: '1'}),
    ],
    jobs: [{project: PROJECT, job_id: 'a'.repeat(32), state: 'failed', label: 'job',
      created_at: '2026-10-15T02:00:00Z', started_at: '2026-10-15T02:00:01Z',
      finished_at: '2026-10-15T02:03:00Z', exit_code: 2, stdout: 'also-secret'}],
  });
  const byLabel = Object.fromEntries(result.tasks.map(task => [task.label, task]));

  assert.deepEqual(byLabel['Task safe'].result, {exit_code: 0});
  assert.equal(Object.hasOwn(byLabel['Task unsafe'], 'result'), false);
  assert.deepEqual(byLabel.job.result, {exit_code: 2});
  assert.equal(JSON.stringify(result).includes('secret'), false);
});
