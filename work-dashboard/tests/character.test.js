'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const model = require('../model.js');
const actor = {id: 'entity:codex:s1', kind: 'agent', sourceKind: 'codex-cli', label: 'Codex'};
const task = {id: 't1', actorId: actor.id, label: 'UI 검사', state: 'running', action: 'test',
  provenance: 'observed', time: '2026-10-16T00:00:00Z'};
const answer = (question, overrides = {}) => model.recordedAnswer({actor, task, tasks: [task], question, ...overrides});
test('a character job follows the object identity, not its current action or label', () => {
  assert.equal(typeof model.characterJob, 'function');
  assert.deepEqual(model.characterJob(actor), model.characterJob({...actor, action: 'review', label: 'Renamed'}));
  assert.equal(model.characterJob(actor).label, '개발자');
  assert.equal(model.characterJob({kind: 'executor'}).label, '운영자');
  assert.equal(model.characterJob({kind: 'mcp'}).label, '도구 담당');
  assert.equal(model.characterJob({...actor, sourceKind: 'chatgpt'}).label, '설계자');
});
test('dialogue reports the selected work and source, without claiming private skill or reasoning', () => {
  assert.equal(typeof model.recordedAnswer, 'function');
  assert.match(answer('current'), /UI 검사/);
  assert.match(answer('current'), /직접 관측/);
  assert.match(answer('current', {task: {...task, provenance: 'reported'}}), /보고/);
  assert.doesNotMatch(answer('current'), /스킬 발동|내부 추론/);
});
test('stale, paused and archived work is described as a last observation', () => {
  for (const state of [{stale: true}, {paused: true}, {archived: true}]) {
    assert.match(answer('current', state), /마지막 기록/);
    assert.match(answer('stop', state), /현재.*확인할 수 없/);
  }
});
test('returned calls and missing outcomes never become a successful command', () => {
  assert.match(answer('result', {task: {...task, state: 'returned'}}), /반환.*실행 성공.*확인/);
  assert.match(answer('result'), /완료 결과.*없/);
  assert.match(answer('stop', {task: {...task, state: 'unknown'}}), /원인.*확인할 수 없/);
  assert.match(answer('stop'), /중단됐다는 기록.*없/);
});
test('stop explanations cite failure and exit evidence, never a guessed root cause', () => {
  assert.match(answer('stop', {task: {...task, state: 'failed', result: {exit_code: 2}}}), /실패.*종료 코드 2/);
  assert.match(answer('stop', {task: {...task, state: 'failed'}}), /원인.*원본 로그/);
});
test('result and related history stay within the selected owner even when other work is newer', () => {
  const own = {...task, id: 'own', state: 'failed', label: '소유자 검사', time: '2026-10-16T00:01:00Z'};
  const other = {...own, id: 'other', actorId: 'other', state: 'succeeded', label: '다른 프로젝트 비밀', time: '2026-10-16T00:02:00Z'};
  const value = answer('result', {tasks: [other, own, task]});
  assert.match(value, /소유자 검사.*실패/);
  assert.doesNotMatch(value, /다른 프로젝트/);
  assert.match(answer('history', {tasks: [other, own, task]}), /2개/);
  assert.match(answer('current', {task: null, tasks: []}), /기록.*없/);
});

test('current host work explains purpose command and observed phase without repeating executor identity', () => {
  const host = {...task, id: 'host-current', kind: 'job', state: 'running', action: 'build',
    label: 'tossApp API·Worker 재빌드', hostExecutionId: 'a'.repeat(32), hostActionKnown: true,
    commandLabel: 'docker compose build api worker', hostPhase: 'EXECUTE',
    workContext: {taskLabel: 'tossApp API·Worker 재빌드', labelSource: 'reported',
      commandLabel: 'docker compose build api worker'}};
  const value = answer('current', {task: host, tasks: [host]});
  assert.match(value, /tossApp API·Worker 재빌드/);
  assert.match(value, /docker compose build api worker/);
  assert.match(value, /명령 실행/);
  assert.doesNotMatch(value, /EXECUTE/);
  assert.match(value, /업무 설명 보고됨/);
  assert.match(value, /실행 상태 직접 관측/);
  assert.doesNotMatch(value, /호스트 실행.*호스트 실행/);
});

test('host lifecycle phases have stable plain Korean labels without inventing business progress', () => {
  const labels = {
    PREPARED: '준비', QUEUED: '대기', PRECHECK: '실행 전 점검', EXECUTE: '명령 실행',
    COMPLETE: '실행 종료', RECOVER: '이전 실행 상태 확인',
  };
  for (const [phase, label] of Object.entries(labels)) assert.equal(model.hostPhaseLabel(phase), label);
  assert.equal(model.hostPhaseLabel('UNKNOWN'), '단계 미수집');
});

test('last result distinguishes an unfinished selected task from a previous completed task', () => {
  const current = {...task, id: 'current', kind: 'job', state: 'running', label: 'API 이미지 빌드',
    hostExecutionId: 'a'.repeat(32), workContext: {taskLabel: 'API 이미지 빌드', labelSource: 'reported', commandLabel: 'docker compose build api'}};
  const previous = {...current, id: 'previous', state: 'succeeded', label: 'Worker 타입 검사',
    time: '2026-10-15T23:59:00Z', result: {exit_code: 0},
    workContext: {taskLabel: 'Worker 타입 검사', labelSource: 'reported', commandLabel: 'npm run typecheck'}};
  const value = answer('result', {task: current, tasks: [current, previous]});
  assert.match(value, /선택한.*완료.*없/);
  assert.match(value, /이전 완료.*Worker 타입 검사.*명령 종료.*종료 코드 0/);
});

test('missing host purpose is explicit and stale dialogue does not claim current execution', () => {
  const host = {...task, kind: 'job', state: 'running', label: 'tossApp 작업 · 목적 미수집',
    hostExecutionId: 'a'.repeat(32), hostActionKnown: false, commandLabel: 'npm test', hostPhase: 'EXECUTE',
    workContext: {taskLabel: null, labelSource: 'unavailable', commandLabel: 'npm test'}};
  const live = answer('current', {task: host, tasks: [host]});
  assert.match(live, /작업 목적.*수집되지 않았/);
  assert.match(live, /npm test/);
  const stale = answer('current', {task: host, tasks: [host], stale: true});
  assert.match(stale, /마지막 기록/);
  assert.doesNotMatch(stale, /현재 실행 중/);
});
