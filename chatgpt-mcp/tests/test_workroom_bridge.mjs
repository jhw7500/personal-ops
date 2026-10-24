import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const SOURCE = readFileSync(path.join(here, '..', 'workroom-bridge.js'), 'utf8');
const JOB = 'a'.repeat(32);
const OPERATION = 'b'.repeat(32);

function fixture({crypto = globalThis.crypto} = {}) {
  const listeners = new Map();
  const frameListeners = new Map();
  const sent = [];
  const child = {postMessage: (message, target) => sent.push({message, target})};
  const window = {
    addEventListener(type, listener) { listeners.set(type, listener); },
    removeEventListener(type, listener) {
      if (listeners.get(type) === listener) listeners.delete(type);
    },
  };
  window.crypto = crypto;
  const context = {window, globalThis: window, structuredClone, console};
  vm.createContext(context);
  vm.runInContext(SOURCE, context, {filename: 'workroom-bridge.js'});
  const iframe = {contentWindow: child, style: {height: ''},
    addEventListener: (type, listener) => frameListeners.set(type, listener),
    removeEventListener: (type, listener) => {
      if (frameListeners.get(type) === listener) frameListeners.delete(type);
    },
    setAttribute(name, value) { this[name] = value; },
  };
  const selected = [];
  const summaries = [];
  const bridge = window.WorkroomBridge.create(iframe, {
    onSummary: summary => summaries.push(structuredClone(summary)),
    onJobSelect: id => selected.push(['job', id]),
    onHostExecutionSelect: id => selected.push(['host', id]),
  });
  const emit = (data, {source = child, origin = 'null'} = {}) =>
    listeners.get('message')?.({data, source, origin});
  const load = () => frameListeners.get('load')?.();
  return {bridge, iframe, sent, selected, summaries, emit, load, child};
}

function snapshot(extra = {}) {
  return {
    project: 'alpha', projectEpoch: 7,
    jobs: [{project: 'alpha', job_id: JOB, state: 'running', created_at: '2026-10-15T00:00:00Z',
      label: '검사', argv: ['--token=private']}],
    events: [{project: 'alpha', call_id: 'call-1', tool: 'read_file', phase: 'started',
      time: '2026-10-15T00:00:00Z', headers: {Authorization: 'secret'}}],
    records: [],
    hostExecutions: [{project: 'alpha', operation_id: OPERATION, kind: 'command', state: 'running',
      phase: 'EXECUTE', created_at: '2026-10-15T00:00:00Z', started_at: '2026-10-15T00:00:01Z',
      finished_at: null, result: null, termination_verified: null,
      authorization: {kind: 'host_access', grant_id: 'c'.repeat(32), expires_at: '2026-10-15T04:00:00Z'},
      private_log: 'never'}],
    hostObservedAt: '2026-10-15T00:00:02Z', following: true, error: '',
    token: 'parent-token', cookie: 'parent-cookie', ...extra,
  };
}

function message(app, fields) {
  const current = app.sent.at(-1).message;
  return {version: 1, epoch: 7, project: 'alpha', nonce: current.nonce, ...fields};
}

test('update sends only allowlisted display metadata with the epoch and frame nonce', () => {
  const app = fixture();
  app.bridge.update(snapshot());
  const delivery = app.sent.at(-1);
  assert.equal(delivery.target, '*');
  assert.equal(delivery.message.type, 'personal-ops:workroom:update');
  assert.equal(delivery.message.epoch, 7);
  assert.equal(typeof delivery.message.nonce, 'string');
  assert.deepEqual(Object.keys(delivery.message).sort(), ['epoch', 'nonce', 'snapshot', 'type', 'version']);
  const text = JSON.stringify(delivery.message);
  for (const secret of ['parent-token', 'parent-cookie', '--token=private', 'Authorization',
    'private_log', 'grant_id', 'profile_digest']) assert.equal(text.includes(secret), false, secret);
  assert.equal(delivery.message.snapshot.jobs[0].job_id, JOB);
  assert.equal(delivery.message.snapshot.hostExecutions[0].operation_id, OPERATION);
  assert.equal(delivery.message.snapshot.hostExecutions[0].display_view, true);
});

test('bridge forwards only valid explicit skill metadata and drops private inputs', () => {
  const app = fixture();
  const input = snapshot({records: [{
    schema_version: 1, project: 'alpha', source_id: 'claude-local', source_kind: 'claude-cli',
    actor_id: 'actor-1', actor_kind: 'agent', actor_label: 'Claude', task_id: 'tool:skill-1',
    task_kind: 'call', task_label: 'Skill', action: 'execute', state: 'started',
    time: '2026-10-15T00:00:00Z', provenance: 'observed',
    skill_id: 'plugin.example:review-pr', skill_name: 'PR 검토',
    tool_input: {skill: 'plugin.example:review-pr', args: 'private-arguments'},
    prompt: 'private-prompt', response: 'private-response',
  }]});

  app.bridge.update(input);

  const record = app.sent.at(-1).message.snapshot.records[0];
  assert.equal(record.skill_id, 'plugin.example:review-pr');
  assert.equal(record.skill_name, 'PR 검토');
  for (const privateText of ['private-arguments', 'private-prompt', 'private-response']) {
    assert.equal(JSON.stringify(record).includes(privateText), false, privateText);
  }
});

test('bridge degrades invalid or unpaired skill metadata to generic records', () => {
  const invalid = [
    {skill_name: 'Review'},
    {skill_id: 'bad/skill', skill_name: 'Review'},
    {skill_id: 7, skill_name: 'Review'},
    {skill_id: 'review', skill_name: 'bad\nlabel'},
    {skill_id: 'review', skill_name: ['Review']},
    {skill_id: 'x'.repeat(121), skill_name: 'Review'},
    {skill_id: 'review', skill_name: 'Review', task_kind: 'agent'},
    {skill_id: 'review', task_kind: 'job'},
  ];
  const app = fixture();
  app.bridge.update(snapshot({records: invalid.map((skill, index) => ({
    schema_version: 1, project: 'alpha', source_id: 'chat', source_kind: 'chatgpt',
    actor_id: `actor-${index}`, actor_kind: 'agent', actor_label: 'Agent',
    task_id: `task-${index}`, task_kind: 'call', task_label: 'Work', action: 'execute',
    state: 'started', time: '2026-10-15T00:00:00Z', provenance: 'reported', ...skill,
  }))}));

  for (const record of app.sent.at(-1).message.snapshot.records) {
    assert.equal(Object.hasOwn(record, 'skill_id'), false);
    assert.equal(Object.hasOwn(record, 'skill_name'), false);
  }
});

test('bridge preserves generic agent and job records without skill metadata', () => {
  const app = fixture();
  app.bridge.update(snapshot({records: ['agent', 'job'].map((taskKind, index) => ({
    schema_version: 1, project: 'alpha', source_id: 'chat', source_kind: 'chatgpt',
    actor_id: `generic-${index}`, actor_kind: 'agent', actor_label: 'Agent',
    task_id: `generic-task-${index}`, task_kind: taskKind, task_label: 'Work', action: 'execute',
    state: 'running', time: '2026-10-15T00:00:00Z', provenance: 'reported',
  }))}));

  assert.deepEqual(
    app.sent.at(-1).message.snapshot.records.map(record => record.task_kind),
    ['agent', 'job'],
  );
});

test('host work purpose and observed phase cross the bridge without execution inputs or credentials', () => {
  const app = fixture();
  const input = snapshot();
  input.hostExecutions[0].work_context = {
    task_label: 'API와 Worker 재빌드', label_source: 'reported',
    command_label: 'npm run build', action: 'build',
    argv: ['--token=nested-secret'], input_text: 'private objective',
  };
  input.hostExecutions[0].argv = ['--password=argument-secret'];
  input.hostExecutions[0].code = 'HOST_COMMAND_FAILED';
  app.bridge.update(input);
  const output = app.sent.at(-1).message.snapshot.hostExecutions[0];
  assert.deepEqual({...output.work_context}, {
    task_label: 'API와 Worker 재빌드', label_source: 'reported',
    command_label: 'npm run build', action: 'build',
  });
  assert.equal(output.phase, 'EXECUTE');
  assert.equal(output.code, 'HOST_COMMAND_FAILED');
  for (const privateText of ['nested-secret', 'argument-secret', 'private objective', 'grant_id']) {
    assert.equal(JSON.stringify(output).includes(privateText), false, privateText);
  }
});

test('invalid host descriptions and phases degrade to a visible legacy operation', () => {
  for (const context of [
    {task_label: 'x'.repeat(161), label_source: 'reported', command_label: 'npm run build', action: 'build'},
    {task_label: 'API\nWorker', label_source: 'reported', command_label: 'npm run build', action: 'build'},
    {task_label: 'API', label_source: 'observed', command_label: 'npm run build', action: 'build'},
    {task_label: null, label_source: 'reported', command_label: 'npm run build', action: 'build'},
    {task_label: 'API', label_source: 'reported', command_label: 'npm run build', action: 'invented'},
  ]) {
    const app = fixture();
    const input = snapshot();
    input.hostExecutions[0].work_context = context;
    input.hostExecutions[0].phase = 'unobserved secret stage';
    input.hostExecutions[0].code = 'Authorization: Bearer private';
    app.bridge.update(input);
    const rows = app.sent.at(-1).message.snapshot.hostExecutions;
    assert.equal(rows.length, 1);
    assert.equal(rows[0].operation_id, OPERATION);
    assert.equal(Object.hasOwn(rows[0], 'work_context'), false);
    assert.equal(Object.hasOwn(rows[0], 'phase'), false);
    assert.equal(Object.hasOwn(rows[0], 'code'), false);
  }
});

test('message receiver rejects forged source, origin, shape, epoch, project and frame nonce', () => {
  const app = fixture();
  app.bridge.update(snapshot());
  const select = message(app, {type: 'personal-ops:workroom:select-job', jobId: JOB});
  app.emit(select, {source: {}, origin: 'null'});
  app.emit(select, {origin: 'http://127.0.0.1'});
  app.emit({...select, extra: true});
  app.emit({...select, epoch: 6});
  app.emit({...select, project: 'beta'});
  app.emit({...select, nonce: 'old-frame'});
  assert.deepEqual(app.selected, []);
});

test('selection accepts only IDs in the current project snapshot', () => {
  const app = fixture();
  app.bridge.update(snapshot());
  app.emit(message(app, {type: 'personal-ops:workroom:select-job', jobId: 'd'.repeat(32)}));
  app.emit(message(app, {type: 'personal-ops:workroom:select-host-execution',
    operationId: 'e'.repeat(32)}));
  assert.deepEqual(app.selected, []);
  app.emit(message(app, {type: 'personal-ops:workroom:select-job', jobId: JOB}));
  app.emit(message(app, {type: 'personal-ops:workroom:select-host-execution', operationId: OPERATION}));
  assert.deepEqual(app.selected, [['job', JOB], ['host', OPERATION]]);
});

test('mutation messages are ignored and summary and height stay bounded', () => {
  const app = fixture();
  app.bridge.update(snapshot());
  app.emit(message(app, {type: 'personal-ops:workroom:api-request', method: 'POST',
    path: '/api/host-access-approve'}));
  app.emit(message(app, {type: 'personal-ops:workroom:summary',
    counts: {active: 2, failed: 1, attention: 0, unknown: 0, total: 3}}));
  app.emit(message(app, {type: 'personal-ops:workroom:height', height: 1200}));
  assert.deepEqual(app.summaries, [{active: 2, failed: 1, attention: 0, unknown: 0, total: 3}]);
  assert.equal(app.iframe.style.height, '1200px');
  app.emit(message(app, {type: 'personal-ops:workroom:height', height: 99999}));
  assert.equal(app.iframe.style.height, '1200px');
});

test('iframe load rotates the nonce and destroy detaches receivers', () => {
  const app = fixture();
  app.bridge.update(snapshot());
  const before = app.sent.at(-1).message.nonce;
  app.load();
  const after = app.sent.at(-1).message.nonce;
  assert.notEqual(after, before);
  app.emit({type: 'personal-ops:workroom:select-job', version: 1, epoch: 7,
    project: 'alpha', nonce: before, jobId: JOB});
  assert.deepEqual(app.selected, []);
  app.bridge.destroy();
  app.emit({type: 'personal-ops:workroom:select-job', version: 1, epoch: 7,
    project: 'alpha', nonce: after, jobId: JOB});
  assert.deepEqual(app.selected, []);
});

test('frame nonces work on LAN HTTP when randomUUID is unavailable', () => {
  let byte = 0;
  const app = fixture({crypto: {getRandomValues(values) {
    for (let index = 0; index < values.length; index += 1) values[index] = byte++;
    return values;
  }}});
  app.bridge.update(snapshot());
  assert.match(app.sent.at(-1).message.nonce, /^[0-9a-f]{32}$/);
  app.load();
  assert.match(app.sent.at(-1).message.nonce, /^[0-9a-f]{32}$/);
});

test('a later frame load is replaced with the exact clean workroom route before data resumes', () => {
  const app = fixture();
  app.bridge.update(snapshot());
  app.load();
  const accepted = app.sent.length;
  app.load();
  assert.equal(app.iframe.src, '/workroom-frame.html');
  assert.equal(app.sent.length, accepted);
  app.load();
  assert.equal(app.sent.length, accepted + 1);
});
