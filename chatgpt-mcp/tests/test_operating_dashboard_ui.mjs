// Operations-dashboard navigation and host-observation integration against the real admin.js.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const ADMIN_JS = readFileSync(path.join(here, '..', 'admin.js'), 'utf8');
const ADMIN_HTML = readFileSync(path.join(here, '..', 'admin.html'), 'utf8');
const MODEL_JS = readFileSync(path.join(here, '..', '..', 'work-dashboard', 'model.js'), 'utf8');
const OPERATION = '3'.repeat(32);
const NOW = Date.parse('2026-10-15T01:10:00Z');

class FakeText {
  constructor(text) { this.textContent = String(text); this.children = []; this.parentNode = null; }
}

class FakeElement {
  constructor(doc, tag, id = '') {
    this.ownerDocument = doc; this.tagName = tag.toUpperCase(); this.id = id;
    this.children = []; this.parentNode = null; this.listeners = {}; this.dataset = {}; this.attributes = {};
    this.hidden = false; this.disabled = false; this.value = ''; this.checked = false;
    this.open = false; this.inert = false; this.className = ''; this.type = ''; this.scrolled = 0;
    const classes = new Set();
    this.classList = {toggle: (name, force) => {
      const on = force ?? !classes.has(name); if (on) classes.add(name); else classes.delete(name); return on;
    }, contains: name => classes.has(name)};
  }
  get textContent() { return this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this.replaceChildren(value === '' ? undefined : new FakeText(value)); }
  append(...nodes) {
    for (const value of nodes) {
      if (value === undefined) continue;
      const node = typeof value === 'string' ? new FakeText(value) : value;
      if (node.parentNode) node.parentNode.children = node.parentNode.children.filter(child => child !== node);
      node.parentNode = this; this.children.push(node);
      if (this.tagName === 'SELECT' && !this.value && node.value) this.value = node.value;
    }
  }
  appendChild(node) { this.append(node); return node; }
  replaceChildren(...nodes) {
    for (const child of this.children) child.parentNode = null;
    this.children = []; if (this.tagName === 'SELECT') this.value = ''; this.append(...nodes);
  }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  async dispatch(type, fields = {}) {
    const event = {type, target: this, currentTarget: this, key: fields.key, preventDefault() {}};
    await Promise.all((this.listeners[type] || []).map(listener => listener(event)));
  }
  click() { return this.dispatch('click'); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  scrollIntoView() { this.scrolled += 1; }
  focus() {}
  select() {}
  querySelectorAll() { return []; }
}

function makeDocument() {
  const doc = {listeners: {}, hidden: false};
  const byId = new Map();
  for (const match of ADMIN_HTML.matchAll(/<([a-zA-Z][a-zA-Z0-9]*)\b([^>]*)>/g)) {
    const id = /\bid="([^"]+)"/.exec(match[2]);
    if (!id) continue;
    const element = new FakeElement(doc, match[1], id[1]);
    element.hidden = /\shidden(?:\s|$|=)/.test(match[2]);
    element.checked = /\schecked(?:\s|$|=)/.test(match[2]);
    element.disabled = /\sdisabled(?:\s|$|=)/.test(match[2]);
    const dataView = /\bdata-view="([^"]+)"/.exec(match[2]);
    if (dataView) element.dataset.view = dataView[1];
    const ariaSelected = /\baria-selected="([^"]+)"/.exec(match[2]);
    if (ariaSelected) element.attributes['aria-selected'] = ariaSelected[1];
    byId.set(id[1], element);
  }
  Object.assign(doc, {
    getElementById: id => byId.get(id) ?? null,
    createElement: tag => new FakeElement(doc, tag), createTextNode: text => new FakeText(text),
    querySelectorAll: selector => selector === '[data-view]' ? [...byId.values()].filter(node => node.dataset.view) : [],
    addEventListener: (type, listener) => { (doc.listeners[type] ||= []).push(listener); },
    execCommand: () => false,
  });
  for (const [child, parent] of [['workroom-frame', 'work-map']]) {
    if (byId.has(child) && byId.has(parent)) byId.get(parent).append(byId.get(child));
  }
  return {doc, byId};
}

const progress = {title: '', status: 'not_recorded', summary: '', completed: [], next_steps: [],
  revision: 0, updated_at: null};

async function settle() {
  for (let index = 0; index < 80; index += 1) await new Promise(resolve => setImmediate(resolve));
}

function hostExecution(project, overrides = {}) {
  return {project, operation_id: OPERATION, kind: 'command', state: 'running', phase: 'EXECUTE',
    code: null, created_at: '2026-10-15T01:00:00Z', started_at: '2026-10-15T01:00:01Z',
    finished_at: null, termination_verified: null, result: null,
    authorization: {kind: 'host_access', grant_id: '4'.repeat(32), expires_at: '2026-10-15T05:00:00Z'},
    ...overrides};
}

async function loadAdmin({pathname = '/', search = '', hostRoute} = {}) {
  const {doc, byId} = makeDocument();
  const updates = []; let options;
  const windowListeners = {};
  let hostCalls = 0;
  let nowMs = NOW;
  const fetch = async target => {
    const url = new URL(target, 'http://127.0.0.1:8765');
    const project = url.searchParams.get('project') || 'alpha';
    const ordinary = {
      '/api/status': {status: 'ok'},
      '/api/session': {connected: true},
      '/api/projects': {projects: [{project: 'alpha', path: '/repo/alpha'},
        {project: 'beta', path: '/repo/beta'}], recommended_project: 'alpha'},
      '/api/memory': {content: '', revision: 0, updated_at: null},
      '/api/progress': progress,
      '/api/jobs': {jobs: [{job_id: 'job-1', state: 'running', preset: 'test', created_at: 'now'}]},
      '/api/activity': {events: [], records: []},
      '/api/changes': {available: false, error: '', files: []},
      '/api/host-operations': {project: 'tossApp', target: 'tarororo-test', plans: []},
      '/api/registered-targets': {project, enabled: false, reason: 'REGISTRY_NOT_CONFIGURED', targets: []},
      '/api/host-target-authorization': {supported: false, enabled: false, reason: 'UNAVAILABLE'},
    };
    if (url.pathname === '/api/host-executions') {
      hostCalls += 1;
      const result = hostRoute ? await hostRoute({project, hostCalls})
        : {status: 200, body: {project, executions: [hostExecution(project)]}};
      return {ok: result.status >= 200 && result.status < 300, status: result.status,
        json: async () => structuredClone(result.body)};
    }
    const body = ordinary[url.pathname];
    return {ok: Boolean(body), status: body ? 200 : 404,
      json: async () => structuredClone(body || {code: 'NOT_FOUND'})};
  };
  class ClockDate extends Date {
    constructor(...args) { super(args.length ? args[0] : nowMs); }
    static now() { return nowMs; }
  }
  const context = {
    document: doc, location: {pathname, search, hash: '#token=login-token'},
    history: {replaceState() {}}, localStorage: {getItem: () => null, setItem() {}},
    matchMedia: () => ({matches: false}), navigator: {}, confirm: () => true,
    fetch, AbortSignal, URL, URLSearchParams, TextEncoder, structuredClone, console, CustomEvent,
    Date: ClockDate,
    queueMicrotask, setTimeout: () => 0, clearTimeout: () => {},
    addEventListener: (type, listener) => { (windowListeners[type] ||= []).push(listener); },
    dispatchEvent: event => { for (const listener of windowListeners[event.type] || []) listener(event); },
    // Model execution belongs to the isolated renderer. This fake renderer only
    // delivers the same read-only summary callback to the real parent controller.
    WorkroomBridge: {create: (_root, value) => { options = value; return {
      update: snapshot => {
        updates.push(structuredClone(snapshot));
        const normalized = context.WorkDashboardModel.snapshot({...snapshot, now: nowMs});
        const executions = normalized.tasks.filter(task => ['job', 'agent'].includes(task.kind));
        value.onSummary({
          active: executions.filter(task => ['started', 'queued', 'running', 'canceling'].includes(task.state)).length,
          failed: executions.filter(task => ['failed', 'timed_out'].includes(task.state)).length,
          attention: executions.filter(task => task.room === 'attention').length,
          unknown: executions.filter(task => task.state === 'unknown').length,
          total: executions.length,
        });
      }, clear() {}, setLive() {},
    }; }},
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(MODEL_JS, context, {filename: 'model.js'});
  vm.runInContext(ADMIN_JS, context, {filename: 'admin.js'});
  await settle();
  return {byId, context, updates, getOptions: () => options, hostCalls,
    advance: milliseconds => { nowMs += milliseconds; }};
}

test('markup provides semantic compact navigation and read-only operating summary', () => {
  for (const id of ['ops-nav', 'nav-overview', 'nav-workroom', 'nav-executions', 'nav-access',
    'nav-repository', 'view-overview', 'view-workroom', 'view-executions', 'view-access',
    'view-repository', 'overview-active-count', 'overview-failure-count', 'overview-access-state',
    'overview-codex-state', 'overview-observation-state', 'work-map', 'workroom-frame']) {
    assert.match(ADMIN_HTML, new RegExp(`id="${id}"`), id);
  }
  assert.match(ADMIN_HTML, /id="ops-nav"[^>]*role="tablist"/);
  assert.match(ADMIN_HTML, /id="view-overview"[^>]*role="tabpanel"/);
  assert.match(ADMIN_HTML, /id="nav-workroom"[^>]*data-view="workroom"[^>]*aria-selected="true"/);
});

test('navigation exposes the selected panel without moving or replacing the frame', async () => {
  const app = await loadAdmin();
  const frame = app.byId.get('workroom-frame');
  const map = app.byId.get('work-map');
  assert.equal(app.byId.get('view-workroom').hidden, false);
  assert.equal(frame.parentNode, map);
  assert.equal(map.hidden, false);
  await app.byId.get('nav-workroom').click();
  assert.equal(app.byId.get('view-overview').hidden, true);
  assert.equal(app.byId.get('view-workroom').hidden, false);
  assert.equal(frame.parentNode, map);
  assert.equal(map.hidden, false);
  assert.equal(app.byId.get('nav-workroom').getAttribute('aria-selected'), 'true');
  await app.byId.get('nav-overview').click();
  assert.equal(map.hidden, true);
  assert.equal(frame.parentNode, map);
  await app.byId.get('nav-access').click();
  assert.equal(map.hidden, true);
  assert.equal(frame.parentNode, map);
});

test('host observations reach the map and a partial failure preserves the last successful snapshot', async () => {
  let fail = false;
  const app = await loadAdmin({hostRoute: ({project}) => fail
    ? {status: 503, body: {code: 'STATE_UNAVAILABLE'}}
    : {status: 200, body: {project, executions: [hostExecution(project)]}}});
  const first = app.updates.at(-1);
  assert.equal(first.hostExecutions[0].operation_id, OPERATION);
  assert.match(first.hostObservedAt, /^\d{4}-/);
  fail = true;
  await app.byId.get('refresh').click();
  await settle();
  const degraded = app.updates.at(-1);
  assert.equal(degraded.hostExecutions[0].operation_id, OPERATION);
  assert.equal(degraded.hostObservedAt, first.hostObservedAt);
  assert.match(degraded.error, /일부/);
  app.byId.get('project').value = 'beta';
  await app.byId.get('project').dispatch('change');
  await settle();
  assert.deepEqual(app.updates.at(-1).hostExecutions, []);
  assert.equal(app.updates.at(-1).hostObservedAt, null);
});

test('a failed host read older than the liveness window cannot leave the overview running count active', async () => {
  let fail = false;
  const app = await loadAdmin({hostRoute: ({project}) => fail
    ? {status: 503, body: {code: 'STATE_UNAVAILABLE'}}
    : {status: 200, body: {project, executions: [hostExecution(project)]}}});
  assert.equal(app.byId.get('overview-active-count').textContent, '1');
  fail = true;
  app.advance(121000);
  await app.byId.get('refresh').click();
  await settle();
  assert.equal(app.byId.get('overview-active-count').textContent, '0');
  assert.equal(app.byId.get('overview-failure-count').textContent, '0');
  assert.match(app.byId.get('overview-observation-state').textContent, /마지막 성공.*현재 조회 지연/);
});

test('map callbacks open the appropriate execution inspector without duplicating the workroom', async () => {
  const app = await loadAdmin();
  app.getOptions().onJobSelect('job-1');
  assert.equal(app.byId.get('view-executions').hidden, false);
  assert.equal(app.byId.get('execution-details').open, true);
  assert.ok(app.byId.get('execution-details').scrolled > 0);
  let selected = null;
  app.context.addEventListener('personal-ops:select-host-execution', event => { selected = event.detail; });
  app.getOptions().onHostExecutionSelect(OPERATION);
  assert.equal(app.byId.get('view-executions').hidden, false);
  assert.deepEqual({...selected}, {project: 'alpha', operationId: OPERATION});
});

test('approval deep links enter Access while ordinary login starts on Workroom', async () => {
  const ordinary = await loadAdmin();
  assert.equal(ordinary.byId.get('view-workroom').hidden, false);
  const deep = await loadAdmin({pathname: '/host-operations',
    search: `?project=alpha&target=tarororo-test&operation_id=${OPERATION}`});
  assert.equal(deep.byId.get('view-access').hidden, false);
  assert.equal(deep.byId.get('nav-access').getAttribute('aria-selected'), 'true');
});
