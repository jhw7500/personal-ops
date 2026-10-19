'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {snapshot, scene, speechEvents, recordedAnswer} = require('../model.js');

const project = 'personal-ops';
const now = Date.parse('2026-10-19T06:00:30Z');

function record(overrides = {}) {
  return {
    schema_version: 1,
    project,
    source_id: 'claude-local',
    source_kind: 'claude-cli',
    actor_id: 'session-a',
    actor_kind: 'agent',
    actor_label: 'Claude',
    task_id: 'skill-call-1',
    task_kind: 'call',
    task_label: 'Review skill',
    action: 'review',
    state: 'started',
    time: '2026-10-19T06:00:10Z',
    provenance: 'observed',
    skill_id: 'plugin:review',
    skill_name: 'PR 검토',
    ...overrides,
  };
}

function canonical(records, instant = now) {
  return snapshot({project, records, now: instant});
}

test('canonical skill metadata follows one invocation through a terminal row that omits it', () => {
  const result = canonical([
    record(),
    record({state: 'returned', time: '2026-10-19T06:00:20Z', skill_id: undefined, skill_name: undefined}),
  ]);

  assert.deepEqual(result.tasks[0].skill, {id: 'plugin:review', name: 'PR 검토', provenance: 'observed'});
  assert.equal(result.tasks[0].state, 'returned');
  assert.equal(result.tasks[0].kind, 'call');
  assert.equal(scene(result).sprites[0].skill.id, 'plugin:review');
});

test('conflicting explicit IDs suppress skill presentation for the whole invocation', () => {
  const result = canonical([
    record(),
    record({state: 'returned', time: '2026-10-19T06:00:20Z', skill_id: 'plugin:deploy', skill_name: '배포'}),
  ]);

  assert.equal(Object.hasOwn(result.tasks[0], 'skill'), false);
  assert.equal(Object.hasOwn(scene(result).sprites[0], 'skill'), false);
});

test('invalid IDs and labels never leak raw strings or become inferred skills', () => {
  const invalidId = '<script>private</script>';
  const privatePrompt = '$review private-customer-name';
  const result = canonical([
    record({skill_id: invalidId, skill_name: '숨은 이름', prompt: privatePrompt}),
    record({task_id: 'skill-call-2', tool_name: 'Skill', skill_id: undefined,
      skill_name: '도구명 추측', prompt: privatePrompt}),
    record({task_id: 'skill-call-3', skill_id: 'plugin:clean', skill_name: 'bad\u0000label'}),
    record({task_id: 'skill-call-4', skill_id: 'plugin:review\n', skill_name: '줄바꿈 ID'}),
  ]);

  const byId = Object.fromEntries(result.tasks.map(task => [task.id, task]));
  assert.equal(Object.hasOwn(byId['task:claude-local:session-a:skill-call-1'], 'skill'), false);
  assert.equal(Object.hasOwn(byId['task:claude-local:session-a:skill-call-2'], 'skill'), false);
  assert.deepEqual(byId['task:claude-local:session-a:skill-call-3'].skill,
    {id: 'plugin:clean', provenance: 'observed'});
  assert.equal(Object.hasOwn(byId['task:claude-local:session-a:skill-call-4'], 'skill'), false);
  const serialized = JSON.stringify(result);
  assert.equal(serialized.includes('private-customer-name'), false);
  assert.equal(serialized.includes(invalidId), false);
  assert.equal(serialized.includes('bad\\u0000label'), false);
});

test('equal skill IDs on distinct calls and owners remain distinct work items', () => {
  const result = canonical([
    record(),
    record({task_id: 'skill-call-2', actor_id: 'session-b', time: '2026-10-19T06:00:11Z'}),
  ]);

  assert.equal(result.tasks.length, 2);
  assert.equal(result.actors.length, 2);
  assert.deepEqual(result.tasks.map(task => task.skill.id), ['plugin:review', 'plugin:review']);
  assert.equal(new Set(result.tasks.map(task => task.id)).size, 2);
});

test('stale skill work becomes unknown and keeps a static factual identity', () => {
  const result = canonical([record()], now + 180000);

  assert.equal(result.tasks[0].state, 'unknown');
  assert.deepEqual(result.tasks[0].skill, {id: 'plugin:review', name: 'PR 검토', provenance: 'observed'});
  assert.equal(result.counts.active, 0);
});

test('skill speech uses the existing lifecycle once and never calls returned work complete', () => {
  const queued = canonical([record({state: 'queued', skill_id: undefined, skill_name: undefined})]);
  const started = canonical([record()]);
  const runningHeartbeat = canonical([record(), record({state: 'running', time: '2026-10-19T06:00:20Z'})]);
  const returned = canonical([
    record(),
    record({state: 'returned', time: '2026-10-19T06:00:20Z', skill_id: undefined, skill_name: undefined}),
  ]);

  assert.deepEqual(speechEvents(queued, started, {now})[0].speech,
    {kind: 'skill-start', tone: 'info', text: 'PR 검토 스킬 발동!', priority: 2, durationMs: 4000});
  assert.deepEqual(speechEvents(started, runningHeartbeat, {now}), []);
  assert.deepEqual(speechEvents(started, returned, {now})[0].speech,
    {kind: 'skill-returned', tone: 'info', text: 'PR 검토 호출 반환', priority: 1, durationMs: 4000});
  assert.doesNotMatch(speechEvents(started, returned, {now})[0].speech.text, /완료|성공/);
});

test('skill failure remains distinct and recorded dialogue names evidence without inventing workflow success', () => {
  const started = canonical([record()]);
  const failed = canonical([
    record(),
    record({state: 'failed', time: '2026-10-19T06:00:20Z', skill_id: undefined, skill_name: undefined}),
  ]);
  const work = failed.tasks[0];
  const actor = failed.actors[0];

  assert.deepEqual(speechEvents(started, failed, {now})[0].speech,
    {kind: 'skill-failed', tone: 'danger', text: 'PR 검토 스킬 실패', priority: 3, durationMs: 6000});
  assert.match(recordedAnswer({actor, task: work, tasks: failed.tasks, question: 'result'}), /스킬 PR 검토 \(plugin:review\)/);
  assert.match(recordedAnswer({actor, task: work, tasks: failed.tasks, question: 'result'}), /직접 관측/);

  const returned = canonical([record(), record({state: 'returned', time: '2026-10-19T06:00:20Z'})]);
  const answer = recordedAnswer({actor: returned.actors[0], task: returned.tasks[0],
    tasks: returned.tasks, question: 'result'});
  assert.match(answer, /스킬 호출.*반환/);
  assert.match(answer, /워크플로 전체의 완료/);
  assert.match(answer, /확인된 것은 아닙니다/);
});

test('inherited reported skill identity does not become observed on a generic terminal row', () => {
  const result = canonical([
    record({provenance: 'reported'}),
    record({state: 'returned', time: '2026-10-19T06:00:20Z', provenance: 'observed',
      skill_id: undefined, skill_name: undefined}),
  ]);

  assert.deepEqual(result.tasks[0].skill,
    {id: 'plugin:review', name: 'PR 검토', provenance: 'reported'});
  assert.equal(result.tasks[0].provenance, 'observed');
  const start = canonical([record({provenance: 'reported'})]);
  const speech = speechEvents(canonical([record({state: 'queued', provenance: 'reported',
    skill_id: undefined, skill_name: undefined})]), start, {now})[0];
  assert.equal(speech.skill.provenance, 'reported');
});

test('skill metadata is call-only even when an earlier call row shares a later agent or job identity', () => {
  const result = canonical([
    record({task_id: 'changes-kind'}),
    record({task_id: 'changes-kind', task_kind: 'agent', state: 'succeeded',
      time: '2026-10-19T06:00:20Z', skill_id: undefined, skill_name: undefined}),
    record({task_id: 'agent-only', task_kind: 'agent', state: 'succeeded', skill_id: 'plugin:review'}),
    record({task_id: 'job-only', task_kind: 'job', state: 'running', skill_id: 'plugin:review'}),
  ]);
  const byId = Object.fromEntries(result.tasks.map(task => [task.id, task]));

  for (const taskId of ['changes-kind', 'agent-only', 'job-only']) {
    assert.equal(Object.hasOwn(byId[`task:claude-local:session-a:${taskId}`], 'skill'), false);
  }
  assert.equal(byId['task:claude-local:session-a:changes-kind'].state, 'succeeded');
  assert.equal(byId['task:claude-local:session-a:changes-kind'].kind, 'agent');
  assert.equal(result.counts.done, 2);
  assert.equal(result.counts.active, 1);
});
