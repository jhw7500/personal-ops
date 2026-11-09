// Execute the real approval card script against a synthetic MCP Apps host.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const html = readFileSync(new URL('../host-access-approval.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const TOKEN = 'a'.repeat(64);
const ROTATED = 'b'.repeat(64);

class Element {
  constructor(id = '') {
    this.id = id; this.value = ''; this.checked = false; this.disabled = false;
    this.hidden = false; this.textContent = ''; this.dataset = {}; this.style = {};
    this.children = []; this.listeners = {};
  }
  get options() { return this.children; }
  append(value) { this.children.push(value); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async dispatch(type) {
    for (const fn of this.listeners[type] || []) await fn({preventDefault() {}});
  }
  querySelector() { return this.submit ||= new Element(); }
  getBoundingClientRect() { return {height: 800}; }
  focus() {}
}

async function settle() {
  for (let i = 0; i < 6; i += 1) await new Promise(resolve => setImmediate(resolve));
}

async function cardApp() {
  const nodes = new Map();
  for (const tag of html.matchAll(/<\w+\b([^>]*)>/g)) {
    const id = /\bid="([^"]+)"/.exec(tag[1])?.[1];
    if (!id) continue;
    const node = new Element(id);
    node.hidden = /\bhidden\b/.test(tag[1]);
    node.disabled = /\bdisabled\b/.test(tag[1]);
    node.checked = /\bchecked\b/.test(tag[1]);
    nodes.set(id, node);
  }
  const listeners = {};
  const calls = [];
  const contexts = [];
  const states = [];
  const value = {
    project: 'alpha', scope: 'installation', card_id: '1'.repeat(32),
    revision: 1, state: 'pending', expires_at: '2099-01-01T00:00:00Z',
    project_root: '/fixture/alpha', lifetime: 'timed', duration_seconds: 60,
    allow_codex: false,
    host_identity: {hostname: 'fixture', user: 'owner', uid: 1000, groups: [],
      filesystem_isolated: false, effective_access: 'host_user_filesystem_network_groups'},
  };
  let token = TOKEN;
  const output = () => ({structuredContent: structuredClone(value),
    ...(value.state === 'pending' ? {_meta: {host_access_confirmation: token}} : {})});
  const emit = data => {
    for (const listener of listeners.message || []) listener({source: parent, data});
  };
  const notify = result => emit({jsonrpc: '2.0',
    method: 'ui/notifications/tool-result', params: result});
  const parent = {postMessage(message) {
    const {id, method, params} = message;
    const reply = result => emit({jsonrpc: '2.0', id, result});
    if (method === 'ui/initialize') {
      reply({hostCapabilities: {updateModelContext: {}}, hostContext: {theme: 'light'}});
    } else if (method === 'ui/notifications/initialized') {
      notify(output());
    } else if (method === 'tools/call') {
      calls.push(structuredClone(params));
      // The external host returns a rotated capability and the requested card
      // options; HostAccess issuer validation is exercised in Python tests.
      assert.equal(params.arguments.confirmation_token, token);
      if (params.name === 'update_host_access_approval') {
        value.revision += 1;
        for (const key of ['lifetime', 'duration_seconds', 'allow_codex', 'allow_git']) {
          value[key] = params.arguments[key];
        }
        token = ROTATED;
      } else if (params.name === 'confirm_host_access_approval') {
        value.state = 'approved';
        value.grant = {grant_id: '2'.repeat(32), project: value.project, scope: value.scope,
          lifetime: value.lifetime, expires_at: value.lifetime === 'until_revoked' ? null : value.expires_at,
          allow_codex: value.allow_codex, allow_git: value.allow_git ?? false};
      }
      reply(output());
    } else if (method === 'ui/update-model-context') {
      contexts.push(structuredClone(params));
      reply({});
    }
  }};
  const context = {
    document: {querySelector: selector => nodes.get(selector.slice(1)),
      createElement: () => new Element(), documentElement: new Element(), body: new Element()},
    parent, innerWidth: 390, console,
    addEventListener: (type, fn) => { (listeners[type] ||= []).push(fn); },
    removeEventListener() {},
    setTimeout: () => 1, clearTimeout() {},
    requestAnimationFrame: fn => { queueMicrotask(fn); return 1; }, cancelAnimationFrame() {},
    ResizeObserver: class {observe() {} disconnect() {}},
    openai: {setWidgetState: state => states.push(structuredClone(state))},
  };
  context.window = context;
  vm.runInNewContext(script, context, {filename: 'host-access-approval.html'});
  await settle();
  return {nodes, calls, contexts, states, value, notify, output};
}

test('Git selection rotates the hidden token and requires explicit confirmation', async () => {
  const app = await cardApp();
  assert.equal(app.nodes.get('git')?.textContent, '별도 승인');
  assert.equal(app.calls.length, 0);
  await app.nodes.get('change').dispatch('click');
  assert.equal(app.nodes.get('approve').disabled, true);
  app.nodes.get('allow-git').checked = true;
  app.nodes.get('lifetime-select').value = 'until_revoked';
  await app.nodes.get('options').dispatch('submit');
  await settle();
  assert.equal(app.calls.length, 1);
  assert.equal(app.calls[0].name, 'update_host_access_approval');
  assert.equal(app.calls[0].arguments.allow_git, true);
  assert.equal(app.calls[0].arguments.allow_codex, false);
  assert.equal(app.calls[0].arguments.duration_seconds, null);
  assert.equal(app.value.state, 'pending');
  assert.equal(app.nodes.get('git').textContent, 'commit·push 반복 승인');
  await app.nodes.get('approve').dispatch('click');
  await settle();
  assert.equal(app.calls[1].name, 'confirm_host_access_approval');
  assert.equal(app.calls[1].arguments.confirmation_token, ROTATED);
  assert.equal(app.contexts[0].structuredContent.hostAccess.allow_git, true);
  assert.equal(app.nodes.get('approve').disabled, true);
  const publicOutput = JSON.stringify([app.contexts, app.states,
    [...app.nodes.values()].map(node => node.textContent)]);
  assert.equal(publicOutput.includes(TOKEN), false);
  assert.equal(publicOutput.includes(ROTATED), false);
});

test('a non-boolean Git permission in a host response locks the approval card', async () => {
  const app = await cardApp();
  assert.equal(app.nodes.get('approve').disabled, false);
  const malformed = app.output();
  malformed.structuredContent.allow_git = 1;
  app.notify(malformed);
  await settle();
  assert.equal(app.nodes.get('approve').disabled, true);
  assert.equal(app.calls.length, 0);
});
