'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const modelPath = path.join(__dirname, '..', 'model.js');
const {snapshot} = require(modelPath);

const PROJECT = 'tossApp';
const NOW = Date.parse('2026-10-09T12:05:00Z');

function event(overrides = {}) {
  return {
    project: PROJECT,
    call_id: 'call-1',
    time: '2026-10-09T12:00:00Z',
    tool: 'read_file',
    phase: 'started',
    ...overrides,
  };
}

function job(overrides = {}) {
  return {
    project: PROJECT,
    job_id: 'a'.repeat(32),
    state: 'running',
    preset: null,
    label: '명령 실행',
    created_at: '2026-10-09T12:00:00Z',
    started_at: '2026-10-09T12:00:01Z',
    finished_at: null,
    exit_code: null,
    ...overrides,
  };
}

test('exports the same pure snapshot API through CommonJS and a browser global', () => {
  assert.equal(typeof snapshot, 'function');

  const source = fs.readFileSync(modelPath, 'utf8');
  const context = {window: {}};
  vm.runInNewContext(source, context, {filename: modelPath});

  assert.equal(typeof context.window.WorkDashboardModel.snapshot, 'function');
});

test('keeps one call work item while unordered duplicate phases advance its state', () => {
  const input = [
    event({phase: 'completed', time: '2026-10-09T12:00:03Z'}),
    event({phase: 'started', time: '2026-10-09T12:00:00Z'}),
    event({phase: 'started', time: '2026-10-09T12:00:01Z'}),
    event({phase: 'failed', time: '2026-10-09T12:00:03Z'}),
  ];

  const result = snapshot({project: PROJECT, events: input, now: NOW});

  assert.deepEqual(result.tasks.map(({id,kind,room,sourceRoom,state,label,time,jobId,tool}) =>
    ({id,kind,room,sourceRoom,state,label,time,jobId,tool})), [{
    id: 'call:call-1',
    kind: 'call',
    room: 'attention',
    sourceRoom: 'analysis',
    state: 'failed',
    label: 'read_file',
    time: '2026-10-09T12:00:03.000Z',
    jobId: null,
    tool: 'read_file',
  }]);
  assert.deepEqual(result.counts, {active: 0, done: 0, attention: 1, unknown: 0, total: 1});
  assert.equal(result.latestEventAt, '2026-10-09T12:00:03.000Z');
});

test('shows a returned start_command call separately from its running job', () => {
  const result = snapshot({
    project: PROJECT,
    now: NOW,
    events: [event({call_id: 'launch', tool: 'start_command', phase: 'completed'})],
    jobs: [job({preset: 'npm run dev'})],
  });

  assert.deepEqual(result.tasks.map(actor => ({id: actor.id, room: actor.room, state: actor.state})), [
    {id: `job:${'a'.repeat(32)}`, room: 'execution', state: 'running'},
    {id: 'call:launch', room: 'done', state: 'returned'},
  ]);
  assert.deepEqual(result.counts, {active: 1, done: 1, attention: 0, unknown: 0, total: 2});
});

test('moves stale pending calls to unknown and ignores malformed timestamps', () => {
  const result = snapshot({
    project: PROJECT,
    now: NOW,
    events: [
      event({call_id: 'stale', time: '2026-10-09T12:02:59Z'}),
      event({call_id: 'fresh', time: '2026-10-09T12:03:01Z'}),
      event({call_id: 'bad-time', time: 'not-a-date'}),
    ],
  });

  assert.deepEqual(result.tasks.map(actor => ({id: actor.id, room: actor.room, state: actor.state})), [
    {id: 'call:fresh', room: 'analysis', state: 'started'},
    {id: 'call:stale', room: 'waiting', state: 'unknown'},
  ]);
  assert.deepEqual(result.counts, {active: 1, done: 0, attention: 0, unknown: 1, total: 2});
});

test('isolates the selected project and excludes polling and unknown tools', () => {
  const result = snapshot({
    project: PROJECT,
    now: NOW,
    events: [
      event({call_id: 'kept', tool: 'replace_content', time: '2026-10-09T12:04:00Z'}),
      event({call_id: 'other-project', project: 'personal-ops'}),
      event({call_id: 'poll', tool: 'get_job'}),
      event({call_id: 'unknown-tool', tool: 'delegate_to_hidden_agent'}),
    ],
    jobs: [job({job_id: 'b'.repeat(32), project: 'personal-ops'})],
  });

  assert.deepEqual(result.tasks.map(actor => actor.id), ['call:kept']);
  assert.equal(result.tasks[0].room, 'coding');
  assert.equal(result.project, PROJECT);
});

test('ignores non-string call IDs instead of coercing them into actor identities', () => {
  const result = snapshot({
    project: PROJECT,
    now: NOW,
    events: [
      event({call_id: null, time: '2026-10-09T12:04:00Z'}),
      event({call_id: 3, time: '2026-10-09T12:04:01Z'}),
      event({call_id: undefined, time: '2026-10-09T12:04:02Z'}),
      event({call_id: 'valid-call', time: '2026-10-09T12:04:03Z'}),
    ],
  });

  assert.deepEqual(result.tasks.map(actor => actor.id), ['call:valid-call']);
  assert.deepEqual(result.counts, {active: 1, done: 0, attention: 0, unknown: 0, total: 1});
});

test('never presents a nonzero succeeded job or cancellation as success', () => {
  const result = snapshot({
    project: PROJECT,
    now: NOW,
    jobs: [
      job({job_id: 'a'.repeat(32), state: 'succeeded', exit_code: 4, finished_at: '2026-10-09T12:04:00Z'}),
      job({job_id: 'b'.repeat(32), state: 'canceled', finished_at: '2026-10-09T12:03:00Z'}),
      job({job_id: 'c'.repeat(32), state: 'succeeded', exit_code: 0, finished_at: '2026-10-09T12:02:00Z'}),
      job({job_id: 'not-hex', state: 'running'}),
    ],
  });

  assert.deepEqual(result.tasks.map(actor => ({id: actor.id, room: actor.room, state: actor.state})), [
    {id: `job:${'a'.repeat(32)}`, room: 'waiting', state: 'unknown'},
    {id: `job:${'b'.repeat(32)}`, room: 'attention', state: 'canceled'},
    {id: `job:${'c'.repeat(32)}`, room: 'done', state: 'succeeded'},
  ]);
  assert.deepEqual(result.counts, {active: 0, done: 1, attention: 1, unknown: 1, total: 3});
});

test('uses testing boundaries and keeps job labels bounded without exposing argv', () => {
  const longLabel = `TypeCheck ${'x'.repeat(240)}`;
  const result = snapshot({
    project: PROJECT,
    now: NOW,
    jobs: [
      job({job_id: 'a'.repeat(32), preset: 'test:e2e'}),
      job({job_id: 'b'.repeat(32), preset: 'contest', label: 'contest runner', argv: ['secret-token']}),
      job({job_id: 'c'.repeat(32), preset: null, label: longLabel}),
    ],
  });

  const byId = Object.fromEntries(result.tasks.map(actor => [actor.id, actor]));
  assert.equal(byId[`job:${'a'.repeat(32)}`].sourceRoom, 'testing');
  assert.equal(byId[`job:${'b'.repeat(32)}`].sourceRoom, 'execution');
  assert.equal(byId[`job:${'c'.repeat(32)}`].sourceRoom, 'testing');
  assert.equal(byId[`job:${'c'.repeat(32)}`].label.length, 200);
  assert.equal(JSON.stringify(result).includes('secret-token'), false);
});

test('retains every work item with active work ahead of terminal history under one executor', () => {
  const jobs = [];
  for (let index = 0; index < 8; index += 1) {
    jobs.push(job({
      job_id: index.toString(16).padStart(32, '0'),
      state: 'running',
      started_at: `2026-10-09T12:00:${String(index).padStart(2, '0')}Z`,
    }));
  }
  for (let index = 8; index < 38; index += 1) {
    jobs.push(job({
      job_id: index.toString(16).padStart(32, '0'),
      state: 'succeeded',
      exit_code: 0,
      finished_at: `2026-10-09T12:00:${String(index).padStart(2, '0')}Z`,
    }));
  }

  const result = snapshot({project: PROJECT, jobs, now: NOW});

  assert.equal(result.tasks.length, 38);
  assert.equal(result.actors.length, 1);
  assert.equal(result.actors[0].activeCount, 8);
  assert.equal(result.tasks.filter(actor => actor.state === 'running').length, 8);
  assert.deepEqual(result.counts, {active: 8, done: 30, attention: 0, unknown: 0, total: 38});
  assert.deepEqual(
    result.tasks.slice(8,24).map(actor => actor.jobId),
    Array.from({length: 16}, (_, offset) => (37 - offset).toString(16).padStart(32, '0')),
  );
});

test('does not mutate event or job inputs', () => {
  const events = [event()];
  const jobs = [job()];
  const before = JSON.stringify({events, jobs});

  snapshot({project: PROJECT, events, jobs, now: NOW});

  assert.equal(JSON.stringify({events, jobs}), before);
});
