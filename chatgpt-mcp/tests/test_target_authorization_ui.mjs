// Target authorization UI contract against the real admin.js with a dependency-free DOM.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const ADMIN_JS = readFileSync(path.join(here, '..', 'admin.js'), 'utf8');
const ADMIN_HTML = readFileSync(path.join(here, '..', 'admin.html'), 'utf8');
const OP = '0123456789abcdef0123456789abcdef';
const TOKEN = 'fresh-admin-token';

class FakeText {
  constructor(text) { this.tagName = '#text'; this.textContent = String(text); this.children = []; }
}

class FakeElement {
  constructor(doc, tag, id = '') {
    this.ownerDocument = doc; this.tagName = tag.toUpperCase(); this.id = id;
    this.children = []; this.listeners = {}; this.dataset = {}; this.attributes = {};
    this.hidden = false; this.disabled = false; this.value = ''; this.checked = false;
    this.open = false; this.inert = false; this.className = ''; this.type = '';
    this.scrollTop = 0; this.scrollHeight = 0;
    const classes = new Set();
    this.classList = {add: name => classes.add(name), remove: name => classes.delete(name),
      contains: name => classes.has(name), toggle: (name, force) => {
        const on = force ?? !classes.has(name); if (on) classes.add(name); else classes.delete(name); return on;
      }};
  }
  get textContent() { return this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this.children = value === '' || value == null ? [] : [new FakeText(value)]; }
  get innerHTML() { throw new Error('innerHTML is forbidden'); }
  set innerHTML(_) { throw new Error('innerHTML is forbidden'); }
  append(...nodes) {
    for (const node of nodes) {
      const child = typeof node === 'string' ? new FakeText(node) : node;
      this.children.push(child);
      if (this.tagName === 'SELECT' && !this.value && child?.value) this.value = child.value;
    }
  }
  appendChild(node) { this.append(node); return node; }
  replaceChildren(...nodes) { this.children = []; this.value = this.tagName === 'SELECT' ? '' : this.value; this.append(...nodes); }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  dispatch(type) {
    const event = {type, target: this, currentTarget: this, preventDefault() {}};
    return Promise.all((this.listeners[type] || []).map(listener => listener(event)));
  }
  click() { return this.dispatch('click'); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  scrollIntoView() {}
  focus() {}
  select() {}
  querySelectorAll() { return []; }
}

function makeDocument() {
  const doc = {created: [], listeners: {}, hidden: false};
  const byId = new Map();
  for (const match of ADMIN_HTML.matchAll(/<([a-zA-Z][a-zA-Z0-9]*)\b([^>]*)>/g)) {
    const id = /\bid="([^"]+)"/.exec(match[2]);
    if (!id) continue;
    const element = new FakeElement(doc, match[1], id[1]);
    element.hidden = /\shidden(\s|$|=)/.test(match[2]);
    element.checked = /\schecked(\s|$|=)/.test(match[2]);
    element.disabled = /\sdisabled(\s|$|=)/.test(match[2]);
    byId.set(id[1], element);
  }
  Object.assign(doc, {
    getElementById: id => byId.get(id) ?? null,
    createElement: tag => { const node = new FakeElement(doc, tag); doc.created.push(node); return node; },
    createTextNode: text => new FakeText(text), querySelectorAll: () => [],
    addEventListener: (type, listener) => { (doc.listeners[type] ||= []).push(listener); },
    execCommand: () => false, write: () => { throw new Error('document.write is forbidden'); },
  });
  return {doc, byId};
}

const emptyProgress = {title: '', status: 'not_recorded', summary: '', completed: [], next_steps: [],
  revision: 0, updated_at: null};

function legacyPlan(authorization) {
  return {operation_id: OP, component: 'schema_update', mode: 'disabled',
    plan_digest: 'b'.repeat(64), source_digest: 'c'.repeat(64),
    expires_at: new Date(Date.now() + 600000).toISOString(),
    impact: {recreate: null, preserve: ['api', 'worker'], schema_change: '006-013'},
    migration_range: ['006', '013'], high_risk_changes: [], changed_path_count: 0,
    source_review: {policy: 'reviewed_snapshot', review_digest: 'e'.repeat(64),
      config_digest: 'f'.repeat(64), inventory_digest: 'a'.repeat(64)}, authorization};
}

async function settle() {
  for (let index = 0; index < 80; index += 1) await new Promise(resolve => setImmediate(resolve));
}

async function loadAdmin({authorization, plan, authorizationError = false} = {}) {
  let current = structuredClone(authorization);
  const {doc, byId} = makeDocument();
  const requests = [];
  const fetch = async (url, options = {}) => {
    const parsed = new URL(url, 'http://127.0.0.1:8765');
    const method = options.method || 'GET';
    const body = options.body ? JSON.parse(options.body) : null;
    requests.push({method, path: parsed.pathname, search: parsed.search, body});
    let status = 200;
    let result;
    const ordinary = {
      '/api/status': {status: 'ok'},
      '/api/projects': {projects: [{project: 'tossApp', path: '/repo/tossApp'}], recommended_project: 'tossApp'},
      '/api/memory': {content: '', revision: 0, updated_at: null},
      '/api/progress': emptyProgress,
      '/api/jobs': {jobs: []}, '/api/activity': {events: [], records: []},
      '/api/changes': {available: false, error: 'Git unavailable', files: []},
      '/api/registered-targets': {project: 'tossApp', enabled: false, reason: 'REGISTRY_NOT_CONFIGURED', targets: []},
      '/api/host-operations': {project: 'tossApp', target: 'tarororo-test',
        planning_policy: {mode: 'reviewed_snapshot', components: ['api', 'worker', 'migration',
          'predecessor_migration', 'schema_update'], commit_required: false, approval: 'target'},
        plans: plan ? [plan] : []},
      '/api/host-operation-review': {project: 'tossApp', target: 'tarororo-test', operation_id: OP,
        review: {version: 1, source_digest: 'a'.repeat(64), files: []}},
    };
    if (parsed.pathname === '/api/session') result = {connected: true};
    else if (parsed.pathname === '/api/host-target-authorization') {
      if (authorizationError) { status = 400; result = {error: 'hidden'}; }
      else if (method === 'POST') {
        current = {...current, enabled: body.enabled,
          grant_id: body.enabled ? '1'.repeat(32) : null,
          granted_at: body.enabled ? '2026-10-14T00:00:00+00:00' : null};
        result = current;
      } else result = current;
    } else if (ordinary[parsed.pathname]) result = ordinary[parsed.pathname];
    else { status = 404; result = {error: 'Not found'}; }
    return {ok: status >= 200 && status < 300, status, json: async () => structuredClone(result)};
  };
  const context = {document: doc, location: {pathname: '/', search: '', hash: '#token=' + TOKEN},
    history: {replaceState() {}}, localStorage: {getItem: () => null, setItem() {}},
    matchMedia: () => ({matches: false}), navigator: {}, confirm: () => true,
    fetch, AbortSignal, URL, URLSearchParams, TextEncoder, structuredClone, console, queueMicrotask,
    setTimeout: () => 0, clearTimeout: () => {}, addEventListener: () => {},
    WorkroomBridge: {create: () => ({update() {}, clear() {}, setLive() {}})}};
  context.window = context;
  vm.createContext(context);
  vm.runInContext(ADMIN_JS, context, {filename: 'admin.js'});
  await settle();
  return {byId, requests, current: () => current};
}

const disabled = {supported: true, enabled: false, project: 'tossApp', target: 'tarororo-test',
  components: ['api', 'worker', 'migration', 'predecessor_migration'], grant_id: null,
  granted_at: null, reason: null};

test('durable authorization can be enabled and revoked without starting an operation', async () => {
  const app = await loadAdmin({authorization: disabled});
  assert.match(app.byId.get('host-target-auth-state').textContent, /허용 안 됨/);
  app.byId.get('host-target-auth-confirmation').value = 'tossApp/tarororo-test';
  app.byId.get('host-target-auth-token').value = TOKEN;
  await app.byId.get('host-target-auth-enable').click();
  await settle();
  assert.equal(app.current().enabled, true);
  assert.match(app.byId.get('host-target-auth-state').textContent, /자동 실행 허용/);
  app.byId.get('host-target-auth-confirmation').value = 'tossApp/tarororo-test';
  app.byId.get('host-target-auth-token').value = TOKEN;
  await app.byId.get('host-target-auth-disable').click();
  await settle();
  assert.equal(app.current().enabled, false);
  assert.equal(app.requests.filter(row => row.method === 'POST').every(row =>
    row.path === '/api/session' || row.path === '/api/host-target-authorization'), true);
});

test('target-authorized combined schema plan explains 006 through 013 and hides one-use approval', async () => {
  const targetMode = {mode: 'target', grant_id: '1'.repeat(32),
    components: ['api', 'worker', 'migration', 'predecessor_migration']};
  const app = await loadAdmin({authorization: {...disabled, enabled: true, grant_id: '1'.repeat(32),
    granted_at: '2026-10-14T00:00:00+00:00'}, plan: legacyPlan(targetMode)});
  assert.equal(app.byId.get('host-approve-form').hidden, true);
  assert.match(app.byId.get('host-details').textContent, /006~013/);
  assert.match(app.byId.get('host-status').textContent, /채팅에서 실행/);
});

test('old per-operation plan keeps its one-use approval even while target grant is enabled', async () => {
  const app = await loadAdmin({authorization: {...disabled, enabled: true, grant_id: '1'.repeat(32),
    granted_at: '2026-10-14T00:00:00+00:00'},
  plan: legacyPlan({mode: 'per_operation'})});
  assert.equal(app.byId.get('host-approve-form').hidden, false);
  assert.equal(app.byId.get('host-approve-submit').disabled, false);
});

test('target plan with a stale grant stays unapprovable and requires a new plan', async () => {
  const app = await loadAdmin({authorization: {...disabled, enabled: true, grant_id: '2'.repeat(32),
    granted_at: '2026-10-14T00:00:00+00:00'},
  plan: legacyPlan({mode: 'target', grant_id: '1'.repeat(32), config_digest: '3'.repeat(64)})});
  assert.equal(app.byId.get('host-approve-form').hidden, true);
  assert.match(app.byId.get('host-status').textContent, /권한이 변경.*사용할 수 없.*새 계획/);
  assert.match(app.byId.get('host-details').textContent, /새 계획/);
});

test('revoking the current grant immediately marks the selected target plan unusable', async () => {
  const grant = '1'.repeat(32);
  const app = await loadAdmin({authorization: {...disabled, enabled: true, grant_id: grant,
    granted_at: '2026-10-14T00:00:00+00:00'},
  plan: legacyPlan({mode: 'target', grant_id: grant, config_digest: '3'.repeat(64)})});
  assert.match(app.byId.get('host-status').textContent, /채팅에서 실행/);
  app.byId.get('host-target-auth-confirmation').value = 'tossApp/tarororo-test';
  app.byId.get('host-target-auth-token').value = TOKEN;
  await app.byId.get('host-target-auth-disable').click();
  await settle();
  assert.equal(app.byId.get('host-approve-form').hidden, true);
  assert.match(app.byId.get('host-status').textContent, /권한이 변경.*새 계획/);
});

test('unsupported or failed authorization load hides mutation controls and keeps buttons disabled', async () => {
  const unsupported = await loadAdmin({authorization: {...disabled, supported: false,
    reason: 'AUTHORIZATION_UNAVAILABLE'}});
  assert.equal(unsupported.byId.get('host-target-auth-controls').hidden, true);
  const failed = await loadAdmin({authorization: disabled, authorizationError: true});
  assert.equal(failed.byId.get('host-target-auth-controls').hidden, true);
  assert.equal(failed.byId.get('host-target-auth-enable').disabled, true);
  assert.equal(failed.byId.get('host-target-auth-disable').disabled, true);
});

test('changed approved migration bytes require a visible regrant without hiding the controls', async () => {
  const app = await loadAdmin({authorization: {...disabled,
    reason: 'MIGRATION_SOURCE_CHANGED'}});
  assert.equal(app.byId.get('host-target-auth-controls').hidden, false);
  assert.equal(app.byId.get('host-target-auth-enable').disabled, false);
  assert.match(app.byId.get('host-target-auth-state').textContent, /SQL.*변경.*다시 허용/);
});
