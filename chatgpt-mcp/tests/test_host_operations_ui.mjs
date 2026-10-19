// Registered host-operation approval UI: admin.js runs in node:vm against a minimal
// hand-written DOM and fetch stub (node built-ins only; no browser, no npm packages).
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
const OTHER = 'fedcba9876543210fedcba9876543210';
const XSS = '<img src=x onerror="globalThis.pwned=1">';
const TOKEN = 'login-token-that-must-never-be-shown-0123456789';
const FORBIDDEN_TAGS = new Set(['IMG', 'SCRIPT', 'IFRAME', 'OBJECT', 'EMBED', 'SVG']);

class FakeText {
  constructor(text) { this.tagName = '#text'; this.textContent = String(text); this.children = []; }
}

class FakeElement {
  constructor(doc, tag, id = '') {
    this.ownerDocument = doc; this.tagName = tag.toUpperCase(); this.id = id;
    this.children = []; this.listeners = {}; this.dataset = {}; this.attributes = {};
    this.hidden = false; this.disabled = false; this.value = ''; this.checked = false;
    this.open = false; this.inert = false; this.className = ''; this.type = '';
    this.scrollTop = 0; this.scrollHeight = 0; this.scrolled = 0;
    const classes = new Set();
    this.classList = {
      add: name => classes.add(name), remove: name => classes.delete(name),
      contains: name => classes.has(name),
      toggle: (name, force) => { const on = force ?? !classes.has(name); if (on) classes.add(name); else classes.delete(name); return on; },
    };
  }
  get textContent() { return this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this.children = value === '' || value == null ? [] : [new FakeText(value)]; }
  get innerHTML() { throw new Error('innerHTML is forbidden'); }
  set innerHTML(_) { throw new Error('innerHTML is forbidden'); }
  get outerHTML() { throw new Error('outerHTML is forbidden'); }
  set outerHTML(_) { throw new Error('outerHTML is forbidden'); }
  insertAdjacentHTML() { throw new Error('insertAdjacentHTML is forbidden'); }
  append(...nodes) {
    for (const node of nodes) {
      const child = typeof node === 'string' ? new FakeText(node) : node;
      this.children.push(child);
      if (this.tagName === 'SELECT' && !this.value && child && child.value) this.value = child.value;
    }
  }
  appendChild(node) { this.append(node); return node; }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  dispatch(type) {
    const event = {type, target: this, currentTarget: this, preventDefault() {}};
    return Promise.all((this.listeners[type] || []).map(listener => listener(event)));
  }
  click() { return this.dispatch('click'); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  scrollIntoView() { this.scrolled += 1; }
  focus() {}
  select() {}
  querySelectorAll() { return []; }
  *walk() { yield this; for (const child of this.children) if (child instanceof FakeElement) yield* child.walk(); }
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
    createElement: tag => { const element = new FakeElement(doc, tag); doc.created.push(element); return element; },
    createTextNode: text => new FakeText(text),
    querySelectorAll: () => [],
    addEventListener: (type, listener) => { (doc.listeners[type] ||= []).push(listener); },
    execCommand: () => false,
    write: () => { throw new Error('document.write is forbidden'); },
  });
  return {doc, byId};
}

const now = Date.now();
const iso = offset => new Date(now + offset).toISOString();
const TARGET_VIEW = {target: 'tarororo-test', environment: 'test', compose_project: 'tarororo-test',
  services: ['api', 'worker', 'db'], db_services: ['db'], actions: ['compose.restart'],
  capabilities: ['compose.restart'], exec_profiles: [], source_review: 'reviewed_snapshot',
  inspect_areas: ['docker']};

function operation(overrides = {}) {
  return {
    kind: 'registered', project: 'tossApp', target: 'tarororo-test', operation_id: OP,
    request_id: 'req-' + XSS, state: 'pending_host_approval', phase: 'APPROVAL', code: null,
    diagnostic_id: null, action: 'compose.restart', parameters: {services: ['api']},
    impact: {recreated: [], interrupted: ['api'], stopped: [], preserved: ['worker', 'db'],
      exec_service: null, volumes: 'preserved', db_connections: 'unaffected'},
    verifications: ['RENDER_POLICY', 'SELECTED_SERVICES_STATE', 'OTHERS_PRESERVED'],
    target_identity: {id: 'tarororo-test', environment: 'test', compose_project: 'tarororo-test',
      services: ['api', 'worker', 'db']},
    source: {review_policy: 'reviewed_snapshot', source_digest: 'c'.repeat(64), git_head: 'd'.repeat(40),
      file_count: 12, changed_paths: ['server/src/' + XSS + '.ts'], changed_path_count: 1,
      high_risk_changes: ['deploy/pc-test/compose.yaml'], review_digest: 'e'.repeat(64)},
    source_digest: 'c'.repeat(64), config_digest: 'f'.repeat(64), executor_digest: 'a'.repeat(64),
    plan_digest: 'b'.repeat(64), created_at: iso(-60000), expires_at: iso(9 * 60000),
    approved_at: null, started_at: null, finished_at: null, result: null,
    approval_url: `/host-operations?project=tossApp&target=tarororo-test&operation_id=${OP}`,
    ...overrides,
  };
}

const REVIEW = {version: 1, git_head: 'd'.repeat(40), source_digest: 'c'.repeat(64), files: [{
  path: 'deploy/pc-test/compose.yaml', change: 'modified', head_size: 10, snapshot_size: 20,
  diff: `--- HEAD/compose.yaml\n+++ snapshot/compose.yaml\n+  command: ${XSS}\n`,
  diff_truncated: false, diff_omitted: null}]};

const PLANNING_POLICY = {mode: 'reviewed_snapshot',
  components: ['api', 'worker', 'migration', 'predecessor_migration'],
  commit_required: false, approval: 'per_operation'};

function legacyPlan(overrides = {}) {
  return {operation_id: OP, component: 'predecessor_migration', mode: 'disabled',
    plan_digest: 'b'.repeat(64), source_digest: 'c'.repeat(64), expires_at: iso(9 * 60000),
    impact: {recreate: null, preserve: ['api', 'worker'], schema_change: '006-012_only'},
    high_risk_changes: ['server/migrations/006_reading_memos.sql'], changed_path_count: 1,
    migration_checksums: {'006': '9'.repeat(64)}, database_generation: '8'.repeat(64),
    source_review: {policy: 'reviewed_snapshot', review_digest: 'e'.repeat(64),
      config_digest: 'f'.repeat(64), inventory_digest: 'a'.repeat(64)}, ...overrides};
}

// Legacy execution inventory and registered reviewed-snapshot inventory intentionally differ.
// The private review binds to source_review.inventory_digest, not plan.source_digest.
const LEGACY_REVIEW = {version: 1, git_head: 'd'.repeat(40), source_digest: 'a'.repeat(64), files: [{
  path: `server/migrations/006_${XSS}.sql`, change: 'added', head_sha256: null,
  snapshot_sha256: '7'.repeat(64), head_mode: null, snapshot_executable: false, head_size: null,
  snapshot_size: 42, diff: `--- /dev/null\n+++ 006.sql\n+select '${XSS}';\n`,
  diff_truncated: false, diff_omitted: null}]};

function events(list = [{cursor: 1, at: iso(-60000), phase: 'APPROVAL', code: 'PLAN_CREATED', fields: {}}], more = false) {
  return {project: 'tossApp', target: 'tarororo-test', operation_id: OP, state: 'pending_host_approval',
    phase: 'APPROVAL', events: list, next_cursor: list.length ? list[list.length - 1].cursor : 0, more};
}

function baseRoutes(state) {
  return {
    'GET /api/status': () => [200, {status: 'ok', version: '1.5.0'}],
    'POST /api/session': () => [200, {connected: true}],
    'GET /api/projects': () => [200, {projects: [{project: 'alpha', path: '/repo/alpha'},
      {project: 'tossApp', path: '/repo/tossApp'}], recommended_project: 'alpha'}],
    'GET /api/memory': () => [200, {content: '', revision: 0, updated_at: null}],
    'GET /api/progress': () => [200, {title: '', status: 'not_recorded', summary: '', completed: [],
      next_steps: [], revision: 0, updated_at: null}],
    'GET /api/jobs': () => [200, {jobs: []}],
    'GET /api/activity': () => [200, {events: [], records: []}],
    'GET /api/changes': () => [200, {available: false, error: 'Git unavailable', files: []}],
    'GET /api/host-operations': () => [200, state.legacy ?? {project: 'tossApp', target: 'tarororo-test',
      planning_policy: {mode: 'git_head', components: ['api'], commit_required: true,
        approval: 'per_operation'}, plans: []}],
    'GET /api/host-operation-review': url => {
      const review = (state.legacyReviews || {})[url.searchParams.get('operation_id')];
      return review === undefined ? [400, {error: 'Invalid request or unavailable state'}]
        : [200, {project: 'tossApp', target: 'tarororo-test',
          operation_id: url.searchParams.get('operation_id'), review}];
    },
    'GET /api/registered-targets': url => [200, {project: url.searchParams.get('project'), enabled: true,
      reason: null, targets: url.searchParams.get('project') === 'tossApp' ? [TARGET_VIEW] : []}],
    'GET /api/registered-operations': () => [200, {project: 'tossApp', target: 'tarororo-test',
      server_time: iso(0), operations: state.operations}],
    'GET /api/registered-operation': url => {
      const selected = state.operations.find(row => row.operation_id === url.searchParams.get('operation_id'));
      return selected ? [200, {project: 'tossApp', target: 'tarororo-test', server_time: iso(0),
        operation: selected, review: state.review ?? null, events: state.events ?? events()}]
        : [400, {error: 'Registered host operation refused', code: 'OPERATION_NOT_FOUND'}];
    },
  };
}

async function settle() {
  for (let index = 0; index < 60; index += 1) await new Promise(resolve => setImmediate(resolve));
}

async function loadAdmin({pathname = '/', search = '', hash = '', state = {}, routes = {}} = {}) {
  state = {operations: [operation()], legacyReviews: {}, ...state};
  const {doc, byId} = makeDocument();
  const requests = [];
  const replaced = [];
  const table = {...baseRoutes(state), ...routes};
  const fetch = async (url, options = {}) => {
    const parsed = new URL(url, 'http://127.0.0.1:8765');
    const method = options.method || 'GET';
    requests.push({method, path: parsed.pathname, search: parsed.search, url,
      headers: {...(options.headers || {})}, body: options.body ? JSON.parse(options.body) : null});
    const handler = table[`${method} ${parsed.pathname}`];
    const [status, body] = handler ? await handler(parsed, options) : [404, {error: 'Not found'}];
    return {ok: status >= 200 && status < 300, status, json: async () => structuredClone(body)};
  };
  const context = {
    document: doc, location: {pathname, search, hash},
    history: {replaceState: (_state, _title, url) => replaced.push(url)},
    localStorage: {getItem: () => null, setItem() {}},
    matchMedia: () => ({matches: false}), navigator: {}, confirm: () => true,
    fetch, AbortSignal, URL, URLSearchParams, TextEncoder, structuredClone, console,
    queueMicrotask, setTimeout: () => 0, clearTimeout: () => {}, addEventListener: () => {},
    WorkroomBridge: {create: () => ({update() {}, clear() {}, setLive() {}})},
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(ADMIN_JS, context, {filename: 'admin.js'});
  await settle();
  return {context, doc, byId, requests, replaced, state, view: context.HostOperationsView};
}

function text(app, id) { return app.byId.get(id).textContent; }
function forbiddenElements(app) { return app.doc.created.filter(element => FORBIDDEN_TAGS.has(element.tagName)); }
const LINK = `?project=tossApp&target=tarororo-test&operation_id=${OP}`;
const LEGACY_LINK = `?project=tossApp&target=tarororo-test&host_operation_id=${OP}`;

async function selectTossApp(app) {
  app.byId.get('project').value = 'tossApp';
  await app.byId.get('project').dispatch('change');
  await settle();
}

test('admin.js never uses HTML parsing sinks', () => {
  for (const sink of ['innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval(', 'new Function']) {
    assert.equal(ADMIN_JS.includes(sink), false, sink);
  }
});

test('legacy approval link selects its plan while the login fragment stays separate', async () => {
  assert.equal(typeof globalThis.HostOperationsView, 'undefined');
  const app = await loadAdmin({pathname: '/host-operations', search: LEGACY_LINK,
    hash: `#token=${TOKEN}`, state: {
    legacy: {project: 'tossApp', target: 'tarororo-test', planning_policy: PLANNING_POLICY,
      plans: [legacyPlan()]}, legacyReviews: {[OP]: LEGACY_REVIEW},
  }});
  assert.equal(typeof app.view.parseLegacyFocus, 'function', 'legacy deep-link parser is missing');
  if (typeof app.view.parseLegacyFocus !== 'function') return;
  assert.deepEqual({...app.view.parseLegacyFocus('/host-operations', LEGACY_LINK)},
    {project: 'tossApp', target: 'tarororo-test', operation_id: OP});
  assert.equal(app.view.parseLegacyFocus('/host-operations', LEGACY_LINK + '&operation_id=' + OTHER), null);
  assert.deepEqual(app.replaced, [`/host-operations${LEGACY_LINK}`]);
  assert.equal(app.byId.get('project').value, 'tossApp');
  assert.equal(app.byId.get('host-plan').value, OP);
  assert.ok(app.byId.get('host-approval').scrolled >= 1);
  assert.equal(app.requests.some(request => request.url.includes('host_operation_id')), false);
  assert.equal(app.requests.some(request => request.url.includes('token=')), false);
  assert.equal(app.requests.filter(request => request.path === '/api/host-operation-review').length, 1);
});

test('Schema014 approval detail identifies its actual DB change and separate execution', async () => {
  const app = await loadAdmin({pathname: '/host-operations', search: LEGACY_LINK,
    hash: `#token=${TOKEN}`, state: {
      legacy: {project: 'tossApp', target: 'tarororo-test', planning_policy: PLANNING_POLICY,
        plans: [legacyPlan({component: 'schema014', impact: {recreate: null,
          preserve: ['api', 'worker'], schema_change: '014_person_relationship_detail.sql'}})]},
      legacyReviews: {[OP]: LEGACY_REVIEW},
    }});
  const detail = JSON.parse(text(app, 'host-details'));
  assert.match(detail.schema_scope, /014/);
  assert.doesNotMatch(detail.schema_scope, /변경 없음/);
  assert.match(detail.execution_effect, /실행은 별도/);
  assert.equal(app.requests.some(request => request.method === 'POST' && request.path !== '/api/session'), false);
});

test('missing legacy approval link never substitutes another pending plan', async () => {
  const app = await loadAdmin({pathname: '/host-operations', search: LEGACY_LINK,
    hash: `#token=${TOKEN}`, state: {
      legacy: {project: 'tossApp', target: 'tarororo-test', planning_policy: PLANNING_POLICY,
        plans: [legacyPlan({operation_id: OTHER})]}, legacyReviews: {[OTHER]: LEGACY_REVIEW},
    }});
  assert.equal(app.byId.get('host-plan').value, '');
  assert.equal(app.byId.get('host-approve-submit').disabled, true);
  assert.match(text(app, 'host-status'), /リンク|링크/);
  assert.equal(app.requests.some(request => request.path === '/api/host-operation-review'), false);
  app.byId.get('host-refresh').click();
  await settle();
  assert.equal(app.byId.get('host-plan').value, '');
  assert.equal(app.byId.get('host-approve-submit').disabled, true);
  app.byId.get('host-plan').value = OTHER;
  await app.byId.get('host-plan').dispatch('change');
  await settle();
  assert.equal(app.byId.get('host-plan').value, OTHER);
  assert.equal(app.byId.get('host-approve-submit').disabled, false);
});

test('reviewed-snapshot policy removes extra scope activation even with no pending plans', async () => {
  const app = await loadAdmin({state: {legacy: {project: 'tossApp', target: 'tarororo-test',
    planning_policy: PLANNING_POLICY, plans: []}}});
  await selectTossApp(app);
  assert.equal(app.byId.has('host-planning-policy'), true, 'planning policy status is missing');
  if (!app.byId.has('host-planning-policy')) return;
  assert.match(text(app, 'host-planning-policy'), /계획.*관리 웹 승인.*실행/);
  assert.match(text(app, 'host-planning-policy'), /커밋 불필요/);
  assert.match(text(app, 'host-planning-policy'), /API.*Worker.*006.*012.*013/);
  assert.equal(app.byId.get('host-scope-controls').hidden, true);
  assert.equal(app.byId.get('host-predecessor-controls').hidden, true);
  assert.equal(app.byId.get('host-approve-submit').disabled, true);
});

test('legacy reviewed-snapshot plan renders trusted review as text and gates approval', async () => {
  const plan = legacyPlan();
  const app = await loadAdmin({state: {legacy: {project: 'tossApp', target: 'tarororo-test',
    planning_policy: PLANNING_POLICY, plans: [plan]}, legacyReviews: {[OP]: LEGACY_REVIEW}}});
  await selectTossApp(app);
  const reviewRequests = app.requests.filter(request => request.path === '/api/host-operation-review');
  assert.equal(reviewRequests.length, 1, 'selected legacy plan review was not loaded');
  assert.deepEqual(Object.fromEntries(new URLSearchParams(reviewRequests[0].search)),
    {project: 'tossApp', operation_id: OP});
  assert.equal(app.byId.has('host-review'), true, 'legacy review container is missing');
  if (!app.byId.has('host-review')) return;
  const rendered = text(app, 'host-details') + text(app, 'host-review');
  for (const expected of ['reviewed_snapshot', 'b'.repeat(64), 'c'.repeat(64), 'e'.repeat(64),
    '7'.repeat(64), '스냅샷 실행 비트 false', '006', '012', '013 제외', XSS, '+select']) {
    assert.ok(rendered.includes(expected), expected);
  }
  assert.equal(app.byId.get('host-approve-submit').disabled, false);
  assert.deepEqual(forbiddenElements(app), []);
  assert.equal(app.context.pwned, undefined);
});

test('empty reviewed snapshot and older git-head plan remain visibly distinct', async () => {
  const empty = await loadAdmin({state: {legacy: {project: 'tossApp', target: 'tarororo-test',
    planning_policy: PLANNING_POLICY, plans: [legacyPlan()]}, legacyReviews: {[OP]: {
      version: 1, git_head: 'd'.repeat(40), source_digest: 'a'.repeat(64), files: []}}}});
  await selectTossApp(empty);
  assert.match(text(empty, 'host-review'), /\(0\).*고위험 변경 없음/);
  assert.equal(empty.byId.get('host-approve-submit').disabled, false);

  const oldPlan = legacyPlan({source_review: undefined});
  const old = await loadAdmin({state: {legacy: {project: 'tossApp', target: 'tarororo-test',
    planning_policy: {mode: 'git_head', components: ['api'], commit_required: true,
      approval: 'per_operation'}, plans: [oldPlan]}}});
  await selectTossApp(old);
  assert.match(text(old, 'host-review'), /기존 Git HEAD 계획.*이전 형식/);
  assert.equal(old.requests.some(request => request.path === '/api/host-operation-review'), false);
  assert.equal(old.byId.get('host-approve-submit').disabled, false);
});

test('legacy review load failure and a stale selection response keep approval disabled', async () => {
  let releaseFirst;
  const first = new Promise(resolve => { releaseFirst = resolve; });
  const secondReview = structuredClone(LEGACY_REVIEW);
  secondReview.files[0].path = 'server/migrations/012_second.sql';
  const firstReview = structuredClone(LEGACY_REVIEW);
  firstReview.files[0].path = 'server/migrations/006_stale.sql';
  const plans = [legacyPlan(), legacyPlan({operation_id: OTHER, component: 'migration',
    impact: {recreate: null, preserve: ['api', 'worker'], schema_change: '013_staged_reading_access.sql'}})];
  const app = await loadAdmin({state: {legacy: {project: 'tossApp', target: 'tarororo-test',
    planning_policy: PLANNING_POLICY, plans}}, routes: {
    'GET /api/host-operation-review': url => url.searchParams.get('operation_id') === OP
      ? first : [200, {project: 'tossApp', target: 'tarororo-test', operation_id: OTHER,
        review: secondReview}],
  }});
  await selectTossApp(app);
  assert.equal(app.byId.get('host-approve-submit').disabled, true);
  app.byId.get('host-plan').value = OTHER;
  await app.byId.get('host-plan').dispatch('change');
  await settle();
  assert.equal(app.byId.get('host-approve-submit').disabled, false);
  assert.ok(text(app, 'host-review').includes('012_second.sql'));
  releaseFirst([200, {project: 'tossApp', target: 'tarororo-test', operation_id: OP,
    review: firstReview}]);
  await settle();
  assert.ok(text(app, 'host-review').includes('012_second.sql'));
  assert.equal(text(app, 'host-review').includes('006_stale.sql'), false);

  const failed = await loadAdmin({state: {legacy: {project: 'tossApp', target: 'tarororo-test',
    planning_policy: PLANNING_POLICY, plans: [legacyPlan()]}}, routes: {
    'GET /api/host-operation-review': () => [400, {error: 'bad /home/secret', code: 'STALE_PLAN'}],
  }});
  await selectTossApp(failed);
  assert.equal(failed.byId.get('host-approve-submit').disabled, true);
  assert.match(text(failed, 'host-status'), /검토 자료.*불러오지 못했습니다/);
  assert.equal(text(failed, 'host-status').includes('/home/secret'), false);
});

test('legacy review rejects an artifact that mismatches the reviewed inventory digest', async () => {
  const wrong = structuredClone(LEGACY_REVIEW);
  wrong.source_digest = '6'.repeat(64);
  const app = await loadAdmin({state: {legacy: {project: 'tossApp', target: 'tarororo-test',
    planning_policy: PLANNING_POLICY, plans: [legacyPlan()]}, legacyReviews: {[OP]: wrong}}});
  await selectTossApp(app);
  assert.equal(app.byId.get('host-approve-submit').disabled, true);
  assert.match(text(app, 'host-status'), /검토 자료를 불러오지 못했습니다/);
  assert.equal(text(app, 'host-review').includes('6'.repeat(64)), false);
});

test('legacy approval keeps the exact authenticated body and never starts execution', async () => {
  const app = await loadAdmin({state: {legacy: {project: 'tossApp', target: 'tarororo-test',
    planning_policy: PLANNING_POLICY, plans: [legacyPlan()]}, legacyReviews: {[OP]: LEGACY_REVIEW}},
    routes: {'POST /api/host-approve': () => [200, {operation_id: OP, approved: true}]}});
  await selectTossApp(app);
  app.byId.get('host-confirmation').value = OP;
  app.byId.get('host-admin-token').value = 'fresh-admin-token';
  await app.byId.get('host-approve-form').dispatch('submit');
  await settle();
  const posts = app.requests.filter(request => request.method === 'POST' && request.path !== '/api/session');
  assert.deepEqual(posts.map(request => request.path), ['/api/host-approve']);
  assert.deepEqual(posts[0].body, {operation_id: OP, plan_digest: 'b'.repeat(64),
    source_digest: 'c'.repeat(64), confirmation: OP, admin_token: 'fresh-admin-token'});
  assert.equal(app.requests.some(request => /execute/.test(request.path)), false);
});

test('legacy schema approval preserves the correct next tool after refreshing plans', async () => {
  for (const [component, tool] of [['schema014', 'execute_schema014_migration'],
    ['schema_update', 'execute_db_migration']]) {
    const app = await loadAdmin({state: {legacy: {project: 'tossApp', target: 'tarororo-test',
      planning_policy: PLANNING_POLICY, plans: [legacyPlan({component})]},
      legacyReviews: {[OP]: LEGACY_REVIEW}},
      routes: {'POST /api/host-approve': () => [200, {operation_id: OP, approved: true}]}});
    await selectTossApp(app);
    app.byId.get('host-confirmation').value = OP;
    app.byId.get('host-admin-token').value = 'fresh-admin-token';
    await app.byId.get('host-approve-form').dispatch('submit');
    await settle();
    assert.ok(text(app, 'host-status').includes(tool), component);
    assert.match(text(app, 'host-status'), /승인 완료/);
    assert.equal(app.requests.some(request => /execute/.test(request.path)), false);
    assert.equal(app.byId.get('host-admin-token').value, '');
  }
});

test('approval link opens the focused operation and strips the login token', async () => {
  const app = await loadAdmin({pathname: '/host-operations', search: LINK, hash: `#token=${TOKEN}`});
  assert.deepEqual(app.replaced, [`/host-operations${LINK}`]);
  assert.equal(app.byId.get('registered-ops').hidden, false);
  assert.ok(app.byId.get('registered-ops').scrolled >= 1);
  assert.equal(app.byId.get('project').value, 'tossApp');
  assert.equal(app.byId.get('registered-target').value, 'tarororo-test');
  assert.match(text(app, 'registered-detail'), new RegExp(OP));
  const detailRequests = app.requests.filter(request => request.path === '/api/registered-operation');
  assert.ok(detailRequests.length >= 1);
  assert.equal(new URLSearchParams(detailRequests[0].search).get('operation_id'), OP);
  for (const request of app.requests) {
    assert.equal(request.url.includes(TOKEN), false, request.url);
    if (request.path !== '/api/session') assert.equal(request.headers.Authorization, undefined, request.path);
  }
  for (const element of app.byId.values()) {
    for (const node of element.walk()) assert.equal(node.textContent.includes(TOKEN), false, element.id);
    assert.equal(String(element.value).includes(TOKEN), false, element.id);
  }
  const plan = operation();
  assert.equal(plan.approval_url.includes('token'), false);
  assert.equal(app.view.parseFocus('/host-operations', LINK + '&token=x'), null);
  assert.equal(app.view.parseFocus('/host-operations', '?project=tossApp&target=tarororo-test&operation_id=XYZ'), null);
  assert.equal(app.view.parseFocus('/', LINK), null);
  assert.deepEqual({...app.view.parseFocus('/host-operations', LINK)},
    {project: 'tossApp', target: 'tarororo-test', operation_id: OP});
});

test('plan, impact, verifications, review and events render as text only', async () => {
  const app = await loadAdmin({pathname: '/host-operations', search: LINK, state: {
    review: REVIEW,
    events: events([
      {cursor: 1, at: iso(-60000), phase: 'APPROVAL', code: 'PLAN_CREATED', fields: {}},
      {cursor: 2, at: iso(-30000), phase: 'APPROVAL', code: XSS, fields: {artifact: XSS}},
    ], true),
  }});
  const detail = text(app, 'registered-detail');
  for (const expected of ['compose.restart', 'api', 'worker', 'db', 'RENDER_POLICY', 'OTHERS_PRESERVED',
    'deploy/pc-test/compose.yaml', 'b'.repeat(64), 'c'.repeat(64), XSS, '+  command: ']) {
    assert.ok(detail.includes(expected), expected);
  }
  assert.match(detail, /남음/);
  assert.ok(text(app, 'registered-events').includes(XSS));
  assert.ok(text(app, 'registered-list').includes('compose.restart'));
  assert.deepEqual(forbiddenElements(app), []);
  assert.equal(app.context.pwned, undefined);
  assert.equal(app.byId.get('registered-events-more').hidden, false);
  assert.equal(app.byId.get('registered-approve').disabled, false);
  assert.equal(app.byId.get('registered-reject').disabled, false);
  await app.byId.get('registered-events-more').click();
  await settle();
  const pages = app.requests.filter(request => request.path === '/api/registered-operation')
    .map(request => new URLSearchParams(request.search).get('cursor'));
  assert.ok(pages.includes('2'), pages.join(','));
});

test('approve posts the exact fresh-auth body and never executes', async () => {
  const app = await loadAdmin({pathname: '/host-operations', search: LINK, routes: {
    'POST /api/registered-approve': () => [200, {operation_id: OP, approved: true, state: 'approved',
      plan_digest: 'b'.repeat(64)}],
  }});
  app.byId.get('registered-confirmation').value = OTHER;
  app.byId.get('registered-admin-token').value = 'fresh-admin-token';
  await app.byId.get('registered-approve').click();
  await settle();
  assert.equal(app.requests.some(request => request.method === 'POST' && request.path === '/api/registered-approve'), false);
  assert.match(text(app, 'registered-status'), /다릅니다/);
  app.byId.get('registered-confirmation').value = OP;
  app.byId.get('registered-admin-token').value = 'fresh-admin-token';
  await app.byId.get('registered-approve').click();
  await settle();
  const posts = app.requests.filter(request => request.method === 'POST' && request.path !== '/api/session');
  assert.equal(posts.length, 1);
  assert.equal(posts[0].path, '/api/registered-approve');
  assert.deepEqual(Object.fromEntries(new URLSearchParams(posts[0].search)), {project: 'tossApp', target: 'tarororo-test'});
  assert.deepEqual(Object.keys(posts[0].body).sort(), ['admin_token', 'confirmation', 'operation_id', 'plan_digest', 'source_digest']);
  assert.deepEqual(posts[0].body, {operation_id: OP, plan_digest: 'b'.repeat(64), source_digest: 'c'.repeat(64),
    confirmation: OP, admin_token: 'fresh-admin-token'});
  assert.equal(app.requests.some(request => /execute/.test(request.path)), false);
  assert.match(text(app, 'registered-status'), /execute_host_operation/);
  assert.equal(app.byId.get('registered-admin-token').value, '');
  assert.equal(app.byId.get('registered-confirmation').value, '');
});

test('reject posts only the operation, typed confirmation and fresh token', async () => {
  const app = await loadAdmin({pathname: '/host-operations', search: LINK, routes: {
    'POST /api/registered-reject': () => [200, operation({state: 'rejected', code: 'HOST_REJECTED'})],
  }});
  app.byId.get('registered-confirmation').value = OP;
  app.byId.get('registered-admin-token').value = 'fresh-admin-token';
  await app.byId.get('registered-reject').click();
  await settle();
  const posts = app.requests.filter(request => request.method === 'POST' && request.path !== '/api/session');
  assert.deepEqual(posts.map(request => request.path), ['/api/registered-reject']);
  assert.deepEqual(posts[0].body, {operation_id: OP, confirmation: OP, admin_token: 'fresh-admin-token'});
});

test('empty state for a registered target without operations', async () => {
  const app = await loadAdmin({state: {operations: []}});
  app.byId.get('project').value = 'tossApp';
  await app.byId.get('project').dispatch('change');
  await settle();
  assert.equal(app.byId.get('registered-ops').hidden, false);
  assert.match(text(app, 'registered-list'), /작업이 없습니다/);
  assert.equal(app.byId.get('registered-approve').disabled, true);
  assert.equal(app.byId.get('registered-reject').disabled, true);
});

test('projects without registered targets keep the panel hidden', async () => {
  const app = await loadAdmin();
  assert.equal(app.byId.get('project').value, 'alpha');
  assert.equal(app.byId.get('registered-ops').hidden, true);
});

test('expired plans show the expired state and cannot be approved', async () => {
  for (const expired of [operation({expires_at: iso(-1000)}),
    operation({state: 'expired', phase: 'APPROVAL', code: 'PLAN_EXPIRED', finished_at: iso(-1000)})]) {
    const app = await loadAdmin({pathname: '/host-operations', search: LINK, state: {operations: [expired]}});
    assert.match(text(app, 'registered-detail'), /만료/);
    assert.equal(app.byId.get('registered-approve').disabled, true);
    assert.equal(app.byId.get('registered-reject').disabled, true);
  }
  const app = await loadAdmin();
  assert.equal(app.view.remaining(iso(-1), now).expired, true);
  assert.equal(app.view.remaining(iso(125000), now).expired, false);
  assert.match(app.view.remaining(iso(125000), now).text, /2분 5초/);
});

test('errors show only code, phase and diagnostic ID', async () => {
  const failed = operation({state: 'failed', phase: 'EXECUTE', code: 'HOST_COMMAND_FAILED',
    diagnostic_id: '9'.repeat(32), result: null, finished_at: iso(-1000)});
  const app = await loadAdmin({pathname: '/host-operations', search: LINK, state: {operations: [failed]}});
  const detail = text(app, 'registered-detail');
  assert.match(detail, /HOST_COMMAND_FAILED/);
  assert.match(detail, /EXECUTE|실행/);
  assert.ok(detail.includes('9'.repeat(32)));
  assert.equal(app.byId.get('registered-approve').disabled, true);
  const broken = await loadAdmin({pathname: '/host-operations', search: LINK, routes: {
    'GET /api/registered-operations': () => [400, {error: 'Registered host operation refused /home/secret/path',
      code: 'INVALID_HOST_CONFIG'}],
  }});
  const status = text(broken, 'registered-availability') + text(broken, 'registered-status');
  assert.match(status, /INVALID_HOST_CONFIG/);
  assert.equal(status.includes('/home/secret/path'), false);
  assert.equal(broken.view.publicError({code: 'bad code <x>', message: '/secret', status: 400}).includes('/secret'), false);
  assert.equal(broken.view.publicError({code: 'bad code <x>', message: '/secret', status: 400}).includes('<x>'), false);
  assert.match(broken.view.publicError({code: 'HOST_OPERATION_FAILED', diagnostic_id: 'a'.repeat(32), status: 400}),
    new RegExp('a'.repeat(32)));
  const disabled = await loadAdmin({pathname: '/host-operations', search: LINK, routes: {
    'GET /api/registered-targets': () => [200, {project: 'tossApp', enabled: false,
      reason: 'HOST_RUNTIME_DISABLED', targets: []}],
  }});
  assert.equal(disabled.byId.get('registered-ops').hidden, false);
  assert.match(text(disabled, 'registered-availability'), /HOST_RUNTIME_DISABLED/);
});

test('completed and running operations show phase, result and recent events', async () => {
  const done = operation({state: 'succeeded', phase: 'COMPLETE', approved_at: iso(-50000), started_at: iso(-40000),
    finished_at: iso(-10000), result: {verified: true, verifications: ['RENDER_POLICY'], services: ['api'], count: 1}});
  delete done.approval_url;
  const running = operation({operation_id: OTHER, state: 'running', phase: 'EXECUTE', created_at: iso(-20000)});
  delete running.approval_url;
  const app = await loadAdmin({pathname: '/host-operations', search: LINK, state: {operations: [running, done]}});
  const list = text(app, 'registered-list');
  assert.match(list, /성공/);
  assert.match(list, /실행 중/);
  const detail = text(app, 'registered-detail');
  assert.match(detail, /성공/);
  assert.match(detail, /RENDER_POLICY/);
  assert.equal(app.byId.get('registered-approve').disabled, true);
});
