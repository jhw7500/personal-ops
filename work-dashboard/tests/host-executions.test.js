'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {snapshot, scene, changes, recordedAnswer, hostCommandLabel, hostExecutionReference,
  hostResultSummary} = require('../model.js');

const project = 'tossApp';
const operation = 'a'.repeat(32);
const observedAt = '2026-10-15T01:10:00Z';
const now = Date.parse('2026-10-15T01:10:30Z');

function hostExecution(overrides = {}) {
  return {
    project,
    operation_id: operation,
    kind: 'command',
    state: 'running',
    phase: 'EXECUTE',
    code: null,
    created_at: '2026-10-15T00:00:00Z',
    started_at: '2026-10-15T00:00:01Z',
    finished_at: null,
    result: null,
    termination_verified: false,
    authorization: {
      kind: 'host_access',
      grant_id: 'b'.repeat(32),
      expires_at: '2026-10-15T02:00:00Z',
    },
    ...overrides,
  };
}

function codexRecord(overrides = {}) {
  return {
    schema_version: 1,
    project,
    source_id: 'personal-ops-agent',
    source_kind: 'codex-cli',
    actor_id: 'codex-tossapp-review',
    actor_kind: 'agent',
    actor_label: 'Codex host agent',
    task_id: operation,
    task_kind: 'agent',
    task_label: 'Host Codex task ' + operation.slice(0, 8),
    action: 'code',
    state: 'running',
    time: '2026-10-15T00:00:01Z',
    provenance: 'observed',
    model: 'gpt-6.1-sol',
    model_source: 'configuration',
    reasoning_effort: 'high',
    reasoning_effort_source: 'configuration',
    ...overrides,
  };
}

function workContext(overrides = {}) {
  return {
    task_label: 'tossApp API·Worker 재빌드',
    label_source: 'reported',
    command_label: 'docker compose build api worker',
    action: 'build',
    ...overrides,
  };
}

test('host work context keeps purpose command phase and action distinct for the same executor', () => {
  const second = 'e'.repeat(32);
  const result = snapshot({project, now, hostObservedAt: observedAt, hostExecutions: [
    hostExecution({work_context: workContext()}),
    hostExecution({operation_id: second, phase: 'PRECHECK', work_context: workContext({
      task_label: 'Schema 013 적용 전 검증', command_label: 'npm run typecheck', action: 'test',
    })}),
  ]});
  const first = result.tasks.find(row => row.hostExecutionId === operation);
  const other = result.tasks.find(row => row.hostExecutionId === second);
  assert.equal(first.label, 'tossApp API·Worker 재빌드');
  assert.deepEqual(first.workContext, {
    taskLabel: 'tossApp API·Worker 재빌드', labelSource: 'reported',
    commandLabel: 'docker compose build api worker',
  });
  assert.equal(first.action, 'build');
  assert.equal(first.hostActionKnown, true);
  assert.equal(first.hostPhase, 'EXECUTE');
  assert.equal(other.label, 'Schema 013 적용 전 검증');
  assert.equal(other.commandLabel, 'npm run typecheck');
  assert.equal(other.action, 'test');
  assert.equal(other.hostPhase, 'PRECHECK');
  assert.equal(new Set([first.actorId, other.actorId]).size, 1);
});

test('reported purpose alone never fabricates a host action or observed step', () => {
  const result = snapshot({project, now, hostObservedAt: observedAt, hostExecutions: [hostExecution({
    work_context: workContext({task_label: '배포 테스트', command_label: null, action: null}),
  })]});
  const task = result.tasks[0];
  assert.equal(task.label, '배포 테스트');
  assert.equal(task.workContext.labelSource, 'reported');
  assert.equal(task.action, 'execute');
  assert.equal(task.hostActionKnown, false);
  assert.equal(task.commandLabel, null);
});

test('missing and legacy host context state that purpose was not collected without exposing identifiers', () => {
  for (const row of [hostExecution(), hostExecution({work_context: {
    task_label: null, label_source: 'unavailable', command_label: 'npm test', action: 'test',
  }})]) {
    const result = snapshot({project, now, hostObservedAt: observedAt, hostExecutions: [row]});
    const task = result.tasks[0];
    assert.match(task.label, /tossApp.*목적 미수집/);
    assert.equal(task.workContext.labelSource, 'unavailable');
    assert.equal(task.workContext.taskLabel, null);
    assert.doesNotMatch(task.label, new RegExp(operation.slice(0, 8)));
  }
});

test('legacy host executions expose safe distinguishing details without inventing a purpose', () => {
  const result = snapshot({project, now, hostObservedAt: observedAt, hostExecutions: [hostExecution({
    state: 'succeeded', phase: 'COMPLETE', finished_at: '2026-10-15T00:20:00Z',
    termination_verified: true,
    result: {exit_code: 0, duration_ms: 103, stdout_bytes: 261, stderr_bytes: 0},
    work_context: {
      task_label: null, label_source: 'unavailable', command_label: 'Host command', action: 'execute',
    },
  })]});
  const task = result.tasks[0];
  assert.equal(hostExecutionReference(task), `실행 ${operation.slice(0, 8)}`);
  assert.equal(hostCommandLabel(task), '호스트 명령 · 종류 미수집');
  assert.match(hostResultSummary(task), /명령 종료.*종료 코드 0/);
  assert.equal(hostResultSummary(task),
    '명령 종료가 확인됐습니다. 종료 코드 0. 추가 업무 결과는 수집되지 않았습니다.');
  const answer = recordedAnswer({actor: result.actors[0], task, tasks: [task], question: 'result'});
  assert.match(answer, /작업 목적 미수집/);
  assert.match(answer, /이 실행을 요청한 업무명은 당시 수집되지 않았/);
  assert.match(answer, new RegExp(`실행 ${operation.slice(0, 8)}`));
  assert.match(answer, /명령 종류: 호스트 명령 · 종류 미수집/);
  assert.match(answer, /관측 단계: 실행 종료/);
  assert.equal((answer.match(/작업 목적 미수집/g) || []).length, 1);
});

test('safe command labels distinguish generic host Python and shell families', () => {
  assert.equal(hostCommandLabel({commandLabel: 'Host command'}), '호스트 명령 · 종류 미수집');
  assert.equal(hostCommandLabel({commandLabel: 'Python command'}), 'Python 실행');
  assert.equal(hostCommandLabel({commandLabel: 'Shell command'}), '셸 실행');
  assert.equal(hostCommandLabel({commandLabel: 'npm run typecheck'}), 'npm run typecheck');
  assert.equal(hostCommandLabel({commandLabel: null}), '명령 종류 미수집');
});

test('unsafe host context fields and malformed phase or code are dropped from the canonical snapshot', () => {
  const result = snapshot({project, now, hostObservedAt: observedAt, hostExecutions: [hostExecution({
    phase: 'EXECUTE\nSECRET', code: 'bad-code', work_context: {
      ...workContext(), raw_argv: ['--token', 'super-secret'], prompt: 'private objective',
    },
  })]});
  const task = result.tasks[0];
  assert.equal(task.workContext.labelSource, 'unavailable');
  assert.equal(task.hostPhase, null);
  assert.equal(task.hostCode, null);
  assert.equal(JSON.stringify(result).includes('super-secret'), false);
  assert.equal(JSON.stringify(result).includes('private objective'), false);
});

test('bounded host failure code is copied without parsing it into a cause', () => {
  const result = snapshot({project, now, hostObservedAt: observedAt, hostExecutions: [hostExecution({
    code: 'PRECHECK_FAILED', work_context: workContext(),
  })]});
  assert.equal(result.tasks[0].hostCode, 'PRECHECK_FAILED');
});

test('printable Unicode context uses codepoint length while blank and control text fail closed', () => {
  const emojiLabel = '🧪'.repeat(81);
  const accepted = snapshot({project, now, hostObservedAt: observedAt, hostExecutions: [hostExecution({
    work_context: workContext({task_label: `  ${emojiLabel}  `}),
  })]}).tasks[0];
  assert.equal(accepted.label, emojiLabel);
  assert.equal(Array.from(accepted.label).length, 81);

  for (const taskLabel of ['   ', `검증\u202E숨김`]) {
    const rejected = snapshot({project, now, hostObservedAt: observedAt, hostExecutions: [hostExecution({
      work_context: workContext({task_label: taskLabel}),
    })]}).tasks[0];
    assert.equal(rejected.workContext.labelSource, 'unavailable');
    assert.equal(rejected.workContext.taskLabel, null);
    assert.doesNotMatch(JSON.stringify(rejected), /숨김/);
  }
});

test('a fresh host observation keeps a long-running command active without changing its transition time', () => {
  const result = snapshot({project, now, hostObservedAt: observedAt,
    hostExecutions: [hostExecution()]});
  assert.equal(result.tasks.length, 1);
  assert.equal(result.tasks[0].state, 'running');
  assert.equal(result.tasks[0].time, '2026-10-15T00:00:01.000Z');
  assert.equal(result.tasks[0].hostExecutionId, operation);
  assert.equal(result.tasks[0].actorId, 'entity:personal-ops-host:host-executor');
  assert.equal(result.actors[0].activeCount, 1);

  const laterPoll = snapshot({project, now: now + 30000, hostObservedAt: '2026-10-15T01:11:00Z',
    hostExecutions: [hostExecution()]});
  assert.equal(laterPoll.tasks[0].time, result.tasks[0].time);
  assert.deepEqual(changes(result, laterPoll, {now: now + 30000}), []);
});

test('a failed or stale host observation never extends active liveness', () => {
  const absent = snapshot({project, now, hostExecutions: [hostExecution()]});
  const stale = snapshot({project, now, hostObservedAt: '2026-10-15T01:07:59Z',
    hostExecutions: [hostExecution()]});
  assert.equal(absent.tasks[0].state, 'unknown');
  assert.equal(stale.tasks[0].state, 'unknown');
  assert.equal(stale.counts.active, 0);
  assert.equal(stale.counts.unknown, 1);
});

test('a prepared host plan stays visibly prepared without fabricating active execution or uncertainty', () => {
  const result = snapshot({project, now, hostObservedAt: observedAt,
    hostExecutions: [hostExecution({state: 'prepared', phase: 'PREPARED', started_at: null,
      termination_verified: null})]});
  assert.equal(result.tasks.length, 1);
  assert.equal(result.tasks[0].state, 'prepared');
  assert.equal(result.tasks[0].room, 'waiting');
  assert.equal(result.counts.active, 0);
  assert.equal(result.counts.unknown, 0);
  assert.equal(result.actors[0].activeCount, 0);
});

test('stale terminal host state remains known only with verified success evidence', () => {
  const completed = hostExecution({state: 'succeeded', phase: 'COMPLETE',
    finished_at: '2026-10-15T00:20:00Z', termination_verified: true,
    result: {exit_code: 0, duration_ms: 1199000, stdout_bytes: 10, stderr_bytes: 0,
      stdout: 'secret output', raw_args: ['secret'], extra: 3}});
  const result = snapshot({project, now, hostObservedAt: '2026-10-15T00:30:00Z', hostExecutions: [completed]});
  assert.equal(result.tasks[0].state, 'succeeded');
  assert.equal(result.tasks[0].room, 'done');
  assert.deepEqual(result.tasks[0].result,
    {exit_code: 0, duration_ms: 1199000, stdout_bytes: 10, stderr_bytes: 0});
  assert.equal(JSON.stringify(result).includes('secret'), false);

  for (const broken of [
    {...completed, termination_verified: false},
    {...completed, result: {...completed.result, exit_code: 1}},
    {...completed, result: null},
  ]) {
    const invalid = snapshot({project, now, hostObservedAt: observedAt, hostExecutions: [broken]});
    assert.equal(invalid.tasks.length, 0);
  }
});

test('host Codex state merges into its canonical record instead of creating a duplicate task or actor', () => {
  const execution = hostExecution({kind: 'codex', agent_metadata: {
    profile_id: 'codex-tossapp-review', model: 'gpt-6.1-sol', reasoning_effort: 'high',
    profile_digest: 'c'.repeat(64), reason: 'WORKFLOW_COMPLEXITY',
  }});
  const result = snapshot({project, now, hostObservedAt: observedAt,
    hostExecutions: [execution], records: [codexRecord()]});
  assert.equal(result.tasks.length, 1);
  assert.equal(result.actors.length, 1);
  assert.equal(result.tasks[0].id,
    `task:personal-ops-agent:codex-tossapp-review:${operation}`);
  assert.equal(result.tasks[0].hostExecutionId, operation);
  assert.equal(result.tasks[0].state, 'running');
  assert.equal(result.tasks[0].telemetry.model.value, 'gpt-6.1-sol');
  assert.equal(result.tasks[0].telemetry.model.source, 'configuration');
  assert.equal(result.tasks[0].telemetry.reasoningEffort.value, 'high');
  assert.equal(Object.hasOwn(result.tasks[0].telemetry, 'usage'), false);
});

test('generic Codex host context describes purpose without inventing an internal coding step', () => {
  const result = snapshot({project, now, hostObservedAt: observedAt, hostExecutions: [hostExecution({
    kind: 'codex', work_context: workContext({task_label: '검토 요청', command_label: 'Codex host task', action: 'code'}),
    agent_metadata: {profile_id: 'codex-tossapp-review', model: 'gpt-6.1-sol', reasoning_effort: 'high',
      profile_digest: 'c'.repeat(64), reason: 'WORKFLOW_COMPLEXITY'},
  })]});
  const task = result.tasks[0];
  assert.equal(task.label, '검토 요청');
  assert.equal(task.action, 'code');
  assert.equal(task.hostActionKnown, false);
  const projected = scene(result);
  assert.equal(projected.sprites.length, 1);
  assert.equal(projected.sprites[0].action, 'execute');
  assert.equal(projected.sprites[0].room, 'execution');
  assert.equal(projected.roles.length, 0);
  const answer = recordedAnswer({actor: result.actors[0], task, tasks: [task], question: 'current'});
  assert.match(answer, /세부 행동 미수신/);
  assert.doesNotMatch(answer, /코드 작성|추론|검토 중/);
});

test('coarse Codex lifecycle telemetry merged with host context does not prove inner code activity', () => {
  const execution = hostExecution({kind: 'codex', work_context: workContext({
    task_label: '검토 요청', command_label: 'Codex host task', action: 'code',
  }), agent_metadata: {
    profile_id: 'codex-tossapp-review', model: 'gpt-6.1-sol', reasoning_effort: 'high',
    profile_digest: 'c'.repeat(64), reason: 'WORKFLOW_COMPLEXITY',
  }});
  const result = snapshot({project, now, hostObservedAt: observedAt, hostExecutions: [execution],
    records: [codexRecord({task_label: '검토 요청', action: 'code', tool_name: null,
      time: '2026-10-15T01:09:50Z'})]});
  const task = result.tasks[0];
  assert.equal(task.label, '검토 요청');
  assert.equal(task.workContext.labelSource, 'reported');
  assert.equal(task.action, 'code');
  assert.equal(task.hostActionKnown, false);
  assert.equal(scene(result).sprites[0].action, 'execute');
  assert.doesNotMatch(recordedAnswer({actor: result.actors[0], task, tasks: [task], question: 'current'}),
    /코드 작성|코드 작업/);
});

test('real Codex tool evidence can prove code activity even when its action matches the host envelope', () => {
  const execution = hostExecution({kind: 'codex', work_context: workContext({
    task_label: '코드 수정 요청', command_label: 'Codex host task', action: 'code',
  }), agent_metadata: {
    profile_id: 'codex-tossapp-review', model: 'gpt-6.1-sol', reasoning_effort: 'high',
    profile_digest: 'c'.repeat(64), reason: 'WORKFLOW_COMPLEXITY',
  }});
  const result = snapshot({project, now, hostObservedAt: observedAt, hostExecutions: [execution],
    records: [codexRecord({task_label: '파일 수정', action: 'code', tool_name: 'replace_content',
      time: '2026-10-15T01:09:50Z'})]});
  assert.equal(result.tasks[0].hostActionKnown, true);
  assert.equal(result.tasks[0].tool, 'replace_content');
  assert.equal(scene(result).sprites[0].action, 'code');
});

test('reported Codex tool activity cannot become observed through a newer host lifecycle envelope', () => {
  const execution = hostExecution({kind: 'codex', work_context: workContext({
    task_label: '회귀 검사 요청', command_label: 'Codex host task', action: 'code',
  }), agent_metadata: {
    profile_id: 'codex-tossapp-review', model: 'gpt-6.1-sol', reasoning_effort: 'high',
    profile_digest: 'c'.repeat(64), reason: 'WORKFLOW_COMPLEXITY',
  }});
  const result = snapshot({project, now, hostObservedAt: observedAt, hostExecutions: [execution],
    records: [codexRecord({task_label: '테스트 실행', action: 'test', tool_name: 'run_tests',
      provenance: 'reported', time: '2026-10-15T01:09:50Z'})]});
  const task = result.tasks[0];
  assert.equal(task.provenance, 'observed');
  assert.equal(task.workContext.labelSource, 'reported');
  assert.equal(task.hostActionKnown, false);
  assert.equal(task.action, 'code');
  assert.equal(task.tool, null);
  assert.equal(scene(result).sprites[0].action, 'execute');
  assert.doesNotMatch(recordedAnswer({actor: result.actors[0], task, tasks: [task], question: 'current'}),
    /검사 중|테스트 실행/);
});

test('a detailed observed Codex action overrides generic host context without changing its purpose', () => {
  const execution = hostExecution({kind: 'codex', work_context: workContext({
    task_label: '회귀 검사 요청', command_label: 'Codex host task', action: 'code',
  }), agent_metadata: {
    profile_id: 'codex-tossapp-review', model: 'gpt-6.1-sol', reasoning_effort: 'high',
    profile_digest: 'c'.repeat(64), reason: 'WORKFLOW_COMPLEXITY',
  }});
  const result = snapshot({project, now, hostObservedAt: observedAt, hostExecutions: [execution],
    records: [codexRecord({task_label: '테스트 실행', action: 'test', time: '2026-10-15T01:09:50Z'})]});
  assert.equal(result.tasks[0].label, '회귀 검사 요청');
  assert.equal(result.tasks[0].action, 'test');
  assert.equal(result.tasks[0].hostActionKnown, true);
  assert.equal(scene(result).sprites[0].room, 'testing');
});

test('fresh explicit host-agent actions and recorded parents survive generic host observations', () => {
  const execution = hostExecution({kind: 'codex', agent_metadata: {
    profile_id: 'codex-tossapp-review', model: 'gpt-6.1-sol', reasoning_effort: 'high',
    profile_digest: 'c'.repeat(64), reason: 'WORKFLOW_COMPLEXITY',
  }});
  const result = snapshot({project, now, hostObservedAt: observedAt, hostExecutions: [execution],
    records: [codexRecord({task_label: '타입 오류 원인 검토', action: 'review',
      time: '2026-10-15T01:09:50Z', parent_source_id: 'chat-session',
      parent_actor_id: 'planner', parent_task_id: 'request'})]});
  const task = result.tasks[0];
  assert.equal(result.tasks.length, 1);
  assert.equal(task.label, '타입 오류 원인 검토');
  assert.equal(task.state, 'running');
  assert.equal(task.action, 'review');
  assert.equal(task.room, 'review');
  assert.equal(task.hostActionKnown, true);
  assert.equal(task.parentTaskId, 'task:chat-session:planner:request');
});

test('an unlabelled host envelope preserves an explicit matched task purpose and its evidence', () => {
  const execution = hostExecution({kind: 'codex', agent_metadata: {
    profile_id: 'codex-tossapp-review', model: 'gpt-6.1-sol', reasoning_effort: 'high',
    profile_digest: 'c'.repeat(64), reason: 'WORKFLOW_COMPLEXITY',
  }});
  const result = snapshot({project, now, hostObservedAt: observedAt, hostExecutions: [execution],
    records: [codexRecord({task_label: '타입 오류 원인 검토', action: 'review',
      time: '2026-10-15T01:09:50Z'})]});
  const task = result.tasks[0];
  assert.equal(task.label, '타입 오류 원인 검토');
  assert.deepEqual(task.workContext, {taskLabel: '타입 오류 원인 검토',
    labelSource: 'observed', commandLabel: null});
  const answer = recordedAnswer({actor: result.actors[0], task, tasks: [task], question: 'current'});
  assert.match(answer, /타입 오류 원인 검토.*업무 설명 직접 관측/);
  assert.doesNotMatch(answer, /작업 목적 미수집/);
});

test('a fresh host task does not refresh an expired detailed reasoning action', () => {
  const execution = hostExecution({kind: 'codex', agent_metadata: {
    profile_id: 'codex-tossapp-review', model: 'gpt-6.1-sol', reasoning_effort: 'high',
    profile_digest: 'c'.repeat(64), reason: 'WORKFLOW_COMPLEXITY',
  }});
  const result = snapshot({project, now, hostObservedAt: observedAt, hostExecutions: [execution],
    records: [codexRecord({task_label: '작업 계획 검토', action: 'reason'})]});
  assert.equal(result.tasks[0].state, 'running');
  assert.equal(result.tasks[0].action, 'code');
  assert.equal(result.tasks[0].hostActionKnown, false);
  assert.equal(result.tasks[0].label, '작업 계획 검토');
});

test('fresh terminal host Codex state overrides an older active record without replaying on later polls', () => {
  const execution = hostExecution({kind: 'codex', state: 'succeeded', phase: 'COMPLETE',
    finished_at: '2026-10-15T01:09:00Z', termination_verified: true,
    result: {exit_code: 0, duration_ms: 4140000, stdout_bytes: 100, stderr_bytes: 0},
    agent_metadata: {profile_id: 'codex-tossapp-review', model: 'gpt-6.1-sol', reasoning_effort: 'high',
      profile_digest: 'c'.repeat(64), reason: 'WORKFLOW_COMPLEXITY'}});
  const before = snapshot({project, now: now - 120000, hostObservedAt: '2026-10-15T01:08:30Z',
    hostExecutions: [{...execution, state: 'running', phase: 'EXECUTE', finished_at: null,
      result: null, termination_verified: false}], records: [codexRecord()]});
  const ended = snapshot({project, now, hostObservedAt: observedAt,
    hostExecutions: [execution], records: [codexRecord()]});
  assert.equal(ended.tasks.length, 1);
  assert.equal(ended.tasks[0].state, 'succeeded');
  assert.equal(ended.tasks[0].time, '2026-10-15T01:09:00.000Z');
  assert.equal(changes(before, ended, {now}).length, 1);

  const repeated = snapshot({project, now: now + 10000, hostObservedAt: '2026-10-15T01:10:40Z',
    hostExecutions: [execution], records: [codexRecord()]});
  assert.deepEqual(changes(ended, repeated, {now: now + 10000}), []);
});

test('an older stale host terminal snapshot does not replace a newer terminal agent record', () => {
  const execution = hostExecution({kind: 'codex', state: 'failed', phase: 'COMPLETE', code: 'OLD_FAILURE',
    finished_at: '2026-10-15T01:08:00Z', termination_verified: true,
    result: {exit_code: 1, duration_ms: 1, stdout_bytes: 0, stderr_bytes: 1},
    agent_metadata: {profile_id: 'codex-tossapp-review', model: 'gpt-6.1-sol', reasoning_effort: 'high',
      profile_digest: 'c'.repeat(64), reason: 'WORKFLOW_COMPLEXITY'}});
  const result = snapshot({project, now, hostObservedAt: '2026-10-15T01:08:01Z', hostExecutions: [execution],
    records: [codexRecord({state: 'succeeded', time: '2026-10-15T01:09:00Z'})]});
  assert.equal(result.tasks.length, 1);
  assert.equal(result.tasks[0].state, 'succeeded');
  assert.equal(result.tasks[0].time, '2026-10-15T01:09:00.000Z');
  assert.equal(result.tasks[0].hostExecutionId, operation);
});

test('a stale host snapshot does not replace a newer fresh active agent record', () => {
  for (const hostState of ['prepared', 'running']) {
    const execution = hostExecution({kind: 'codex', state: hostState,
      phase: hostState === 'prepared' ? 'PREPARED' : 'EXECUTE',
      started_at: hostState === 'prepared' ? null : '2026-10-15T01:00:00Z',
      termination_verified: hostState === 'prepared' ? null : false,
      agent_metadata: {profile_id: 'codex-tossapp-review', model: 'gpt-6.1-sol', reasoning_effort: 'high',
        profile_digest: 'c'.repeat(64), reason: 'WORKFLOW_COMPLEXITY'}});
    const result = snapshot({project, now, hostObservedAt: '2026-10-15T01:00:00Z', hostExecutions: [execution],
      records: [codexRecord({time: '2026-10-15T01:10:00Z', received_at: '2026-10-15T01:10:00Z'})]});
    assert.equal(result.tasks.length, 1);
    assert.equal(result.tasks[0].state, 'running');
    assert.equal(result.tasks[0].time, '2026-10-15T01:10:00.000Z');
    assert.equal(result.tasks[0].hostExecutionId, operation);
    assert.equal(result.counts.active, 1);
  }
});

test('other-project and malformed host executions cannot create work or false success', () => {
  const valid = hostExecution();
  const values = [
    {...valid, project: 'another'},
    {...valid, operation_id: '../escape'},
    {...valid, state: 'success'},
    {...valid, termination_verified: 'true'},
    {...valid, started_at: 'not-a-time'},
    {...valid, kind: 'codex', agent_metadata: {profile_id: '../bad'}},
  ];
  const result = snapshot({project, now, hostObservedAt: observedAt, hostExecutions: values});
  assert.equal(result.tasks.length, 0);
  assert.equal(result.actors.length, 0);
});

test('authority-sanitized display observations do not require grant IDs or profile digests', () => {
  const command = hostExecution();
  const codex = hostExecution({operation_id: 'e'.repeat(32), kind: 'codex', agent_metadata: {
    profile_id: 'codex-tossapp-review', model: 'gpt-6.1-sol', reasoning_effort: 'high',
    profile_digest: 'd'.repeat(64),
  }});
  const publicRows = [command, codex].map(row => ({
    project: row.project, operation_id: row.operation_id, kind: row.kind, state: row.state,
    created_at: row.created_at, started_at: row.started_at, finished_at: row.finished_at,
    result: row.result, termination_verified: row.termination_verified, display_view: true,
    ...(row.agent_metadata ? {agent_metadata: {
      profile_id: row.agent_metadata.profile_id, model: row.agent_metadata.model,
      reasoning_effort: row.agent_metadata.reasoning_effort,
    }} : {}),
  }));
  const result = snapshot({project, now, hostObservedAt: observedAt, hostExecutions: publicRows});
  assert.equal(result.tasks.length, 2);
  assert.ok(result.tasks.some(task => task.hostExecutionId === command.operation_id));
  assert.ok(result.tasks.some(task => task.hostExecutionId === codex.operation_id));
});

test('one general host executor groups many operations while preserving legacy job identifiers', () => {
  const legacyId = 'd'.repeat(32);
  const result = snapshot({project, now, hostObservedAt: observedAt,
    hostExecutions: [hostExecution(), hostExecution({operation_id: 'e'.repeat(32), state: 'queued', phase: 'QUEUED'})],
    jobs: [{project, job_id: legacyId, state: 'running', label: 'legacy', preset: null,
      created_at: '2026-10-15T01:09:00Z', started_at: '2026-10-15T01:09:01Z',
      finished_at: null, exit_code: null}],
  });
  const hostTasks = result.tasks.filter(row => row.hostExecutionId);
  assert.equal(hostTasks.length, 2);
  assert.equal(new Set(hostTasks.map(row => row.actorId)).size, 1);
  assert.equal(result.actors.find(row => row.id === 'entity:personal-ops-host:host-executor').taskCount, 2);
  assert.equal(result.tasks.find(row => row.jobId === legacyId).id, `job:${legacyId}`);
});
