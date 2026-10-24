// Expiring host-access UI contract with a dependency-free synthetic DOM/fetch harness.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const UI_JS = readFileSync(path.join(here, '..', 'host-access-admin.js'), 'utf8');
const ADMIN_HTML = readFileSync(path.join(here, '..', 'admin.html'), 'utf8');
const REQUEST = '1'.repeat(32);
const GRANT = '2'.repeat(32);
const OPERATION = '3'.repeat(32);
const XSS = '<img src=x onerror="globalThis.pwned=1">';
const NOW = Date.parse('2026-10-15T04:00:00Z');

class FakeText {
  constructor(value) { this.textContent = String(value); this.children = []; }
}

class FakeElement {
  constructor(tag, id = '') {
    this.tagName = tag.toUpperCase(); this.id = id; this.children = []; this.listeners = {};
    this.hidden = false; this.disabled = false; this.value = ''; this.checked = false; this.dataset = {};
  }
  get textContent() { return this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this.children = value === '' ? [] : [new FakeText(value)]; }
  get innerHTML() { throw new Error('innerHTML is forbidden'); }
  set innerHTML(_) { throw new Error('innerHTML is forbidden'); }
  insertAdjacentHTML() { throw new Error('insertAdjacentHTML is forbidden'); }
  append(...children) {
    this.children.push(...children);
    if (this.tagName === 'SELECT' && !this.value && children[0]?.value) this.value = children[0].value;
  }
  replaceChildren(...children) { this.children = []; this.value = ''; this.append(...children); }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  async dispatch(type) {
    const event = {type, target: this, preventDefault() {}};
    await Promise.all((this.listeners[type] || []).map(listener => listener(event)));
  }
}

function makeDocument() {
  const byId = new Map();
  for (const match of ADMIN_HTML.matchAll(/<([a-zA-Z][a-zA-Z0-9]*)\b([^>]*)>/g)) {
    const id = /\bid="([^"]+)"/.exec(match[2]);
    if (!id) continue;
    const element = new FakeElement(match[1], id[1]);
    element.hidden = /\shidden(?:\s|>|=)/.test(match[2]);
    element.disabled = /\sdisabled(?:\s|>|=)/.test(match[2]);
    element.checked = /\schecked(?:\s|>|=)/.test(match[2]);
    element.value = /\bvalue="([^"]*)"/.exec(match[2])?.[1] || '';
    byId.set(id[1], element);
  }
  return {
    byId,
    document: {
      hidden: false,
      getElementById: id => byId.get(id) || null,
      createElement: tag => new FakeElement(tag),
      write: () => { throw new Error('document.write is forbidden'); },
    },
  };
}

const access = (overrides = {}) => ({
  project: 'alpha', scope: 'installation', active: false, reason: 'NO_GRANT',
  requests: [{request_id: REQUEST, expires_at: '2026-10-15T05:00:00+00:00'}], ...overrides,
  host_identity: {hostname: 'build-host', user: 'jhw', uid: 1003,
    groups: ['docker', 'sudo', 'users'], filesystem_isolated: false,
    effective_access: 'host_user_filesystem_network_groups', passwordless_sudo_verified: false},
});
const execution = (overrides = {}) => ({
  project: 'alpha', operation_id: OPERATION, kind: 'command', state: 'running', phase: 'EXECUTE',
  code: null, created_at: '2026-10-15T01:00:00+00:00', started_at: '2026-10-15T01:00:01+00:00',
  finished_at: null, ...overrides,
});

async function settle() {
  for (let index = 0; index < 20; index += 1) await new Promise(resolve => setImmediate(resolve));
}

async function loadUi({routes = {}, linkFocus = null} = {}) {
  const {document, byId} = makeDocument();
  const requests = [];
  const intervals = [];
  const observers = [];
  const windowListeners = {};
  let nowMs = NOW;
  const defaults = {
    'GET /api/host-access': url => [200, access({project: url.searchParams.get('project')})],
    'GET /api/host-executions': url => [200, {project: url.searchParams.get('project'),
      executions: [execution({project: url.searchParams.get('project')})]}],
    'GET /api/host-execution': url => [200, execution({project: url.searchParams.get('project')})],
    'GET /api/host-execution-log': url => [200, {text: `safe ${XSS}\n`, next_cursor: 20, eof: true,
      stream: url.searchParams.get('stream')}],
    'POST /api/host-access-approve': () => [200, {grant_id: GRANT, approved: true}],
    'POST /api/host-access-revoke': () => [200, {revoked: true}],
    'POST /api/host-execution-cancel': () => [200, {cancelled: true}],
  };
  const table = {...defaults, ...routes};
  const fetch = async (target, options = {}) => {
    const url = new URL(target, 'http://127.0.0.1:8765');
    const method = options.method || 'GET';
    const record = {method, path: url.pathname, url, signal: options.signal,
      body: options.body ? JSON.parse(options.body) : null};
    requests.push(record);
    const handler = table[`${method} ${url.pathname}`];
    const [status, body] = handler ? await handler(url, options, record) : [404, {code: 'NOT_FOUND'}];
    return {ok: status >= 200 && status < 300, status, json: async () => structuredClone(body)};
  };
  class MutationObserver {
    constructor(callback) { this.callback = callback; observers.push(this); }
    observe() {}
  }
  class ClockDate extends Date {
    constructor(...args) { super(args.length ? args[0] : nowMs); }
    static now() { return nowMs; }
  }
  const context = {
    PersonalOpsHostAccessFocus: linkFocus,
    document, fetch, URL, AbortController, console,
    MutationObserver,
    setInterval: (callback, delay) => { intervals.push({callback, delay}); return intervals.length; },
    clearInterval() {},
    addEventListener: (type, listener) => { (windowListeners[type] ||= []).push(listener); },
    dispatchEvent: event => { for (const listener of windowListeners[event.type] || []) listener(event); },
    CustomEvent, Date: ClockDate,
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(UI_JS, context, {filename: 'host-access-admin.js'});
  byId.get('dashboard').hidden = false;
  byId.get('project').value = 'alpha';
  await byId.get('project').dispatch('change');
  await settle();
  return {byId, requests, intervals, observers, context,
    advance: milliseconds => { nowMs += milliseconds; }};
}

const text = (app, id) => app.byId.get(id).textContent;
const calls = (app, method, endpoint) => app.requests.filter(
  request => request.method === method && request.path === endpoint);

test('markup describes the real trust boundary and exposes independent hooks', () => {
  for (const hook of ['host-access', 'host-access-state', 'host-access-identity', 'host-access-request',
    'host-access-lifetime', 'host-access-duration', 'host-access-codex',
    'host-access-confirmation', 'host-access-token',
    'host-access-approve', 'host-access-revoke', 'host-execution', 'host-execution-state',
    'host-execution-stream', 'host-execution-log', 'host-execution-cancel']) {
    assert.match(ADMIN_HTML, new RegExp(`id="${hook}"`), hook);
  }
  assert.match(ADMIN_HTML, /설치.*전체/);
  assert.match(ADMIN_HTML, /호스트 사용자/);
  assert.match(ADMIN_HTML, /격리.*아닙/);
  assert.match(ADMIN_HTML, /되돌리|롤백/);
  assert.match(ADMIN_HTML, /Codex 계정.*모델 사용량/);
  assert.match(ADMIN_HTML, /passwordless sudo.*검증.*아닙/);
  assert.match(ADMIN_HTML, /host-access-admin\.js/);
  assert.equal(UI_JS.includes('innerHTML'), false);
  assert.equal(UI_JS.includes('insertAdjacentHTML'), false);
});

test('a work-map host selection can open a just-created execution before the list poll catches up', async () => {
  const pending = '4'.repeat(32);
  const app = await loadUi({routes: {
    'GET /api/host-execution': url => [200, execution({
      project: url.searchParams.get('project'), operation_id: url.searchParams.get('operation_id'),
      state: url.searchParams.get('operation_id') === pending ? 'queued' : 'running',
    })],
  }});
  app.context.dispatchEvent(new CustomEvent('personal-ops:select-host-execution', {
    detail: {project: 'alpha', operationId: pending},
  }));
  await settle();
  assert.equal(app.byId.get('host-execution').value, pending);
  assert.match(text(app, 'host-execution-state'), new RegExp(pending));
  assert.match(text(app, 'host-execution-state'), /queued/);
  const detail = calls(app, 'GET', '/api/host-execution').at(-1);
  assert.equal(detail.url.searchParams.get('operation_id'), pending);
});

test('deep link selects the exact request without granting access automatically', async () => {
  const alternate = '9'.repeat(32);
  const app = await loadUi({linkFocus: {project: 'alpha', request_id: REQUEST}, routes: {
    'GET /api/host-access': () => [200, access({requests: [
      {request_id: alternate, expires_at: '2026-10-15T05:00:00+00:00'},
      {request_id: REQUEST, expires_at: '2026-10-15T05:00:00+00:00'},
    ]})],
  }});
  assert.equal(app.byId.get('host-access-request').value, REQUEST);
  assert.equal(calls(app, 'POST', '/api/host-access-approve').length, 0);
  assert.equal(app.byId.get('host-access-approve').disabled, true);
});

test('expired deep link never silently selects another pending request', async () => {
  const alternate = '9'.repeat(32);
  const app = await loadUi({linkFocus: {project: 'alpha', request_id: REQUEST}, routes: {
    'GET /api/host-access': () => [200, access({requests: [
      {request_id: alternate, expires_at: '2026-10-15T05:00:00+00:00'},
    ]})],
  }});
  assert.equal(app.byId.get('host-access-request').value, '');
  assert.match(text(app, 'host-access-guidance'), /링크의 요청이 만료/);
  app.byId.get('host-access-confirmation').value = 'alpha';
  app.byId.get('host-access-token').value = 'fresh-token';
  await app.byId.get('host-access-token').dispatch('input');
  assert.equal(app.byId.get('host-access-approve').disabled, true);
  await app.byId.get('host-access-approve').dispatch('click');
  await settle();
  assert.equal(calls(app, 'POST', '/api/host-access-approve').length, 0);
});

test('loads access, executions, selected status and bounded log as text', async () => {
  const app = await loadUi();
  assert.match(text(app, 'host-access-state'), /승인 대기.*1/);
  assert.match(text(app, 'host-access-identity'), /build-host.*jhw.*1003.*docker.*sudo/s);
  assert.match(text(app, 'host-access-identity'), /파일시스템.*네트워크.*그룹/s);
  assert.match(text(app, 'host-access-identity'), /passwordless sudo.*검증되지 않았/s);
  assert.equal(app.byId.get('host-access-request').value, REQUEST);
  assert.equal(app.byId.get('host-execution').value, OPERATION);
  assert.match(text(app, 'host-execution-state'), /running/);
  assert.equal(text(app, 'host-execution-log'), `safe ${XSS}\n`);
  assert.equal(app.context.pwned, undefined);
  for (const endpoint of ['/api/host-access', '/api/host-executions',
    '/api/host-execution', '/api/host-execution-log']) {
    assert.equal(calls(app, 'GET', endpoint).length, 1, endpoint);
  }
});

test('a cached grant expires locally while following is paused without making a network request', async () => {
  const app = await loadUi({routes: {
    'GET /api/host-access': () => [200, access({active: true, reason: null, requests: [],
      grant_id: GRANT, expires_at: '2026-10-15T04:01:00Z', allow_codex: true})],
  }});
  assert.match(text(app, 'overview-access-state'), /^활성 ·/);
  assert.equal(text(app, 'overview-codex-state'), '호스트 Codex 허용 · 격리 준비 상태와 별개');
  app.byId.get('host-access-confirmation').value = 'alpha';
  app.byId.get('host-access-token').value = 'still-fresh-token';
  await app.byId.get('host-access-confirmation').dispatch('input');
  await app.byId.get('host-access-token').dispatch('input');
  assert.equal(app.byId.get('host-access-revoke').disabled, false);
  const reads = calls(app, 'GET', '/api/host-access').length;
  app.byId.get('dashboard').dataset.following = 'false';
  app.advance(61000);
  await app.intervals[0].callback();
  await settle();
  assert.equal(calls(app, 'GET', '/api/host-access').length, reads);
  assert.equal(text(app, 'overview-access-state'), '액세스 만료됨');
  assert.equal(text(app, 'overview-codex-state'), '호스트 Codex 허용 안 함 · 격리 준비 상태와 별개');
  assert.doesNotMatch(text(app, 'host-access-state'), /^활성/);
  assert.match(text(app, 'host-access-state'), /만료/);
  assert.equal(app.byId.get('host-access-revoke').disabled, true);
  assert.equal(app.byId.get('host-access-confirmation').value, 'alpha');
  assert.equal(app.byId.get('host-access-token').value, 'still-fresh-token');
});

test('a thirty-day grant stays active after twenty-nine days and shows time in days', async () => {
  const app = await loadUi({routes: {
    'GET /api/host-access': () => [200, access({active: true, reason: null, requests: [],
      grant_id: GRANT, expires_at: new Date(NOW + 30 * 86400000).toISOString(), allow_codex: false})],
  }});
  assert.equal(text(app, 'overview-access-state'), '활성 · 30일 0시간 남음');
  app.byId.get('dashboard').dataset.following = 'false';
  app.advance(29 * 86400000);
  await app.intervals[0].callback();
  await settle();
  assert.equal(text(app, 'overview-access-state'), '활성 · 1일 0시간 남음');
  app.byId.get('host-access-confirmation').value = 'alpha';
  app.byId.get('host-access-token').value = 'revoke-token';
  await app.byId.get('host-access-token').dispatch('input');
  assert.equal(app.byId.get('host-access-revoke').disabled, false);
  await app.byId.get('host-access-revoke').dispatch('click');
  await settle();
  assert.equal(calls(app, 'POST', '/api/host-access-revoke')[0].body.grant_id, GRANT);
});

test('missing or malformed host identity prevents approval even with a fresh token', async () => {
  for (const hostIdentity of [undefined, {hostname: 'build-host'}, {
    hostname: 'build-host', user: 'jhw', uid: 1003, groups: ['docker'],
    filesystem_isolated: true, effective_access: 'host_user_filesystem_network_groups',
    passwordless_sudo_verified: false,
  }]) {
    const app = await loadUi({routes: {
      'GET /api/host-access': () => {
        const value = access();
        if (hostIdentity === undefined) delete value.host_identity;
        else value.host_identity = hostIdentity;
        return [200, value];
      },
    }});
    app.byId.get('host-access-confirmation').value = 'alpha';
    app.byId.get('host-access-token').value = 'fresh-token';
    await app.byId.get('host-access-confirmation').dispatch('input');
    await app.byId.get('host-access-token').dispatch('input');
    assert.equal(app.byId.get('host-access-approve').disabled, true);
    assert.match(text(app, 'host-access-identity'), /확인할 수 없어.*승인할 수 없습니다/);
    await app.byId.get('host-access-approve').dispatch('click');
    await settle();
    assert.equal(calls(app, 'POST', '/api/host-access-approve').length, 0);
  }
});

test('approval uses selected request, thirty-day default, Codex consent, project confirmation and fresh token', async () => {
  const app = await loadUi();
  app.byId.get('host-access-codex').checked = true;
  app.byId.get('host-access-confirmation').value = 'alpha';
  app.byId.get('host-access-token').value = 'fresh-token';
  await app.byId.get('host-access-confirmation').dispatch('input');
  await app.byId.get('host-access-token').dispatch('input');
  assert.equal(app.byId.get('host-access-approve').disabled, false);
  await app.byId.get('host-access-approve').dispatch('click');
  await settle();
  const approve = calls(app, 'POST', '/api/host-access-approve');
  assert.equal(approve.length, 1);
  assert.deepEqual(approve[0].body, {
    request_id: REQUEST, lifetime: 'timed', duration_seconds: 2592000, allow_codex: true,
    confirmation: 'alpha', admin_token: 'fresh-token',
  });
  assert.equal(app.byId.get('host-access-token').value, '');
  assert.equal(app.requests.some(request => request.path.includes('start')), false);
});

test('approval submits a thirty-day duration', async () => {
  const app = await loadUi();
  app.byId.get('host-access-confirmation').value = 'alpha';
  app.byId.get('host-access-token').value = 'fresh-token';
  app.byId.get('host-access-duration').value = '43200';
  await app.byId.get('host-access-confirmation').dispatch('input');
  await app.byId.get('host-access-token').dispatch('input');
  await app.byId.get('host-access-duration').dispatch('input');
  assert.equal(app.byId.get('host-access-approve').disabled, false);
  await app.byId.get('host-access-approve').dispatch('click');
  await settle();
  assert.equal(calls(app, 'POST', '/api/host-access-approve')[0].body.duration_seconds, 2592000);
});

test('until-revoked request submits a null duration and remains locally active', async () => {
  const app = await loadUi({routes: {
    'GET /api/host-access': () => [200, access({version: 2, requests: [{
      request_id: REQUEST, expires_at: '2026-10-15T05:00:00+00:00',
      lifetime: 'until_revoked', grant_state_revision: 1,
    }]})],
  }});
  assert.equal(app.byId.get('host-access-lifetime').value, 'until_revoked');
  assert.equal(app.byId.get('host-access-duration').disabled, true);
  app.byId.get('host-access-confirmation').value = 'alpha';
  app.byId.get('host-access-token').value = 'fresh-token';
  await app.byId.get('host-access-confirmation').dispatch('input');
  await app.byId.get('host-access-token').dispatch('input');
  assert.equal(app.byId.get('host-access-approve').disabled, false);
  await app.byId.get('host-access-approve').dispatch('click');
  await settle();
  assert.deepEqual(calls(app, 'POST', '/api/host-access-approve')[0].body, {
    request_id: REQUEST, lifetime: 'until_revoked', duration_seconds: null,
    allow_codex: false, confirmation: 'alpha', admin_token: 'fresh-token',
  });

  const durable = await loadUi({routes: {
    'GET /api/host-access': () => [200, access({
      version: 2, active: true, reason: null, requests: [], grant_id: GRANT,
      lifetime: 'until_revoked', expires_at: null, allow_codex: false,
    })],
  }});
  assert.equal(text(durable, 'overview-access-state'), '활성 · 소유자가 취소할 때까지');
  assert.match(text(durable, 'host-access-state'), /소유자가 취소할 때까지/);
});

test('approval rejects durations above thirty days', async () => {
  const overLimit = await loadUi();
  overLimit.byId.get('host-access-confirmation').value = 'alpha';
  overLimit.byId.get('host-access-token').value = 'fresh-token';
  overLimit.byId.get('host-access-duration').value = '43201';
  await overLimit.byId.get('host-access-confirmation').dispatch('input');
  await overLimit.byId.get('host-access-token').dispatch('input');
  await overLimit.byId.get('host-access-duration').dispatch('input');
  assert.equal(overLimit.byId.get('host-access-approve').disabled, true);
  await overLimit.byId.get('host-access-approve').dispatch('click');
  await settle();
  assert.equal(calls(overLimit, 'POST', '/api/host-access-approve').length, 0);
});

test('revoke and cancel use current identifiers and exact project confirmation', async () => {
  const app = await loadUi({routes: {
    'GET /api/host-access': () => [200, access({active: true, reason: null, requests: [],
      grant_id: GRANT, expires_at: '2026-10-15T05:00:00+00:00', allow_codex: true})],
  }});
  assert.match(text(app, 'host-access-state'), /활성.*Codex 허용/);
  app.byId.get('host-access-confirmation').value = 'alpha';
  app.byId.get('host-access-token').value = 'revoke-token';
  await app.byId.get('host-access-token').dispatch('input');
  await app.byId.get('host-access-confirmation').dispatch('input');
  assert.equal(app.byId.get('host-access-revoke').disabled, false);
  await app.byId.get('host-access-revoke').dispatch('click');
  await settle();
  assert.deepEqual(calls(app, 'POST', '/api/host-access-revoke')[0].body,
    {grant_id: GRANT, confirmation: 'alpha', admin_token: 'revoke-token'});

  app.byId.get('host-access-confirmation').value = 'alpha';
  app.byId.get('host-access-token').value = 'cancel-token';
  await app.byId.get('host-access-token').dispatch('input');
  await app.byId.get('host-access-confirmation').dispatch('input');
  assert.equal(app.byId.get('host-execution-cancel').disabled, false);
  await app.byId.get('host-execution-cancel').dispatch('click');
  await settle();
  assert.deepEqual(calls(app, 'POST', '/api/host-execution-cancel')[0].body,
    {operation_id: OPERATION, confirmation: 'alpha', admin_token: 'cancel-token'});
});

test('revoked status and absent requests direct the owner back to ChatGPT', async () => {
  const app = await loadUi({routes: {
    'GET /api/host-access': () => [200, access({reason: 'REVOKED', requests: []})],
  }});
  assert.match(text(app, 'host-access-state'), /취소됨/);
  assert.match(text(app, 'host-access-guidance'), /ChatGPT.*request_host_access/);
});

test('project switching aborts an in-flight token action and ignores its stale response', async () => {
  let releaseApproval;
  const approval = new Promise(resolve => { releaseApproval = resolve; });
  const app = await loadUi({routes: {
    'POST /api/host-access-approve': async () => {
      await approval;
      return [200, {grant_id: GRANT, approved: true}];
    },
  }});
  app.byId.get('host-access-confirmation').value = 'alpha';
  app.byId.get('host-access-token').value = 'single-use-token';
  await app.byId.get('host-access-confirmation').dispatch('input');
  await app.byId.get('host-access-token').dispatch('input');
  await app.byId.get('host-access-approve').dispatch('click');
  await settle();
  const pending = calls(app, 'POST', '/api/host-access-approve')[0];
  assert.equal(pending.signal.aborted, false);
  app.byId.get('project').value = 'beta';
  await app.byId.get('project').dispatch('change');
  await settle();
  assert.equal(pending.signal.aborted, true);
  assert.equal(app.byId.get('host-access-token').value, '');
  releaseApproval();
  await settle();
  assert.doesNotMatch(text(app, 'host-access-action-status'), /승인했습니다/);
});

test('project changes abort old reads and prevent stale project rendering', async () => {
  let releaseAlpha;
  const alpha = new Promise(resolve => { releaseAlpha = resolve; });
  const app = await loadUi({routes: {
    'GET /api/host-access': async url => {
      if (url.searchParams.get('project') === 'alpha') await alpha;
      return [200, access({project: url.searchParams.get('project'), requests: []})];
    },
    'GET /api/host-executions': async url => {
      if (url.searchParams.get('project') === 'alpha') await alpha;
      const project = url.searchParams.get('project');
      return [200, {project, executions: project === 'beta' ? [execution({project,
        operation_id: '4'.repeat(32), state: 'succeeded'})] : []}];
    },
    'GET /api/host-execution': url => [200, execution({
      project: url.searchParams.get('project'),
      operation_id: url.searchParams.get('operation_id'), state: 'succeeded',
    })],
  }});
  app.byId.get('project').value = 'beta';
  await app.byId.get('project').dispatch('change');
  await settle();
  assert.ok(calls(app, 'GET', '/api/host-access')[0].signal.aborted);
  releaseAlpha();
  await settle();
  assert.equal(app.byId.get('host-execution').value, '4'.repeat(32));
  assert.doesNotMatch(text(app, 'host-execution-state'), /alpha/);
  assert.equal(app.byId.get('host-access-token').value, '');
  assert.equal(app.byId.get('host-access-confirmation').value, '');
});

test('polling is every three seconds and refreshes selected stderr incrementally', async () => {
  let cursor = 0;
  const app = await loadUi({routes: {
    'GET /api/host-execution-log': url => {
      cursor = Number(url.searchParams.get('cursor'));
      return [200, {content: cursor === 0 ? 'first\n' : 'second\n',
        next_cursor: cursor === 0 ? 6 : 13, eof: true}];
    },
  }});
  assert.equal(app.intervals.length, 1);
  assert.equal(app.intervals[0].delay, 3000);
  assert.equal(text(app, 'host-execution-log'), 'first\n');
  app.byId.get('host-execution-stream').value = 'stderr';
  await app.byId.get('host-execution-stream').dispatch('change');
  await settle();
  let latest = calls(app, 'GET', '/api/host-execution-log').at(-1);
  assert.equal(latest.url.searchParams.get('stream'), 'stderr');
  assert.equal(latest.url.searchParams.get('cursor'), '0');
  await app.intervals[0].callback();
  await settle();
  latest = calls(app, 'GET', '/api/host-execution-log').at(-1);
  assert.equal(latest.url.searchParams.get('cursor'), '6');
  assert.equal(text(app, 'host-execution-log'), 'first\nsecond\n');
});

test('a failed status poll clears fresh credentials and disables stale actions', async () => {
  let reads = 0;
  const app = await loadUi({routes: {
    'GET /api/host-access': () => {
      reads += 1;
      return reads === 1
        ? [200, access({active: true, reason: null, requests: [], grant_id: GRANT,
          expires_at: '2026-10-15T05:00:00+00:00', allow_codex: false})]
        : [503, {code: 'STATE_UNAVAILABLE'}];
    },
  }});
  app.byId.get('host-access-confirmation').value = 'alpha';
  app.byId.get('host-access-token').value = 'must-not-survive';
  await app.byId.get('host-access-token').dispatch('input');
  await app.byId.get('host-access-confirmation').dispatch('input');
  assert.equal(app.byId.get('host-access-revoke').disabled, false);
  await app.intervals[0].callback();
  await settle();
  assert.equal(app.byId.get('host-access-revoke').disabled, true);
  assert.equal(app.byId.get('host-execution-cancel').disabled, true);
  assert.equal(app.byId.get('host-access-token').value, '');
  assert.match(text(app, 'host-access-action-status'), /STATE_UNAVAILABLE/);
});
