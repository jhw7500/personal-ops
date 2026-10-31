import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const html = fs.readFileSync(new URL("../workspace-access-approval.html", import.meta.url), "utf8");
const source = html.match(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/)?.[1];
const REQUEST = "1".repeat(32);
const TOKEN = "2".repeat(64);
const DIGEST = "3".repeat(64);

const pending = (overrides = {}) => ({
  request_id: REQUEST, project: "alpha", path: "/workspace/outside/result.json",
  scope_kind: "file", operations: ["read"], duration_seconds: 900,
  purpose: "검토한 결과 파일 읽기", request_expires_at: "2099-10-21T12:00:00Z",
  authority_epoch: 7, policy_digest: DIGEST, status: "pending", scope: "installation",
  ...overrides,
});
const approved = (overrides = {}) => ({
  grant_id: "4".repeat(32), project: "alpha", expires_at: "2099-10-21T12:15:00Z",
  request_id: REQUEST, scope_path: "/workspace/outside/result.json", operations: ["read"],
  authority_epoch: 7, policy_digest: DIGEST, status: "approved", ...overrides,
});
const cancelled = () => ({request_id: REQUEST, status: "cancelled", authority_epoch: 7,
  policy_digest: DIGEST});
const result = (value, token) => ({content: [{type: "text", text: "bounded"}],
  structuredContent: value, ...(token ? {_meta: {workspace_confirmation: token}} : {})});

class Element {
  constructor(id = "") { this.id = id; this.disabled = false; this.listeners = {};
    this.dataset = {}; this.style = {}; this.children = []; }
  get textContent() { return this.children.join(""); }
  set textContent(value) { this.children = [String(value)]; }
  get innerHTML() { throw new Error("innerHTML forbidden"); }
  set innerHTML(_) { throw new Error("innerHTML forbidden"); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async dispatch(type) { await Promise.all((this.listeners[type] || []).map(fn => fn({type}))); }
  getBoundingClientRect() { return {height: 480}; }
}

async function load({legacy = true, output = pending(), token = TOKEN, callTool} = {}) {
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  const elements = new Map(ids.map(id => [id, new Element(id)]));
  const root = new Element("root"), body = new Element("body");
  const calls = [], posted = [], states = [], listeners = {};
  const parent = {postMessage: value => posted.push(structuredClone(value))};
  const document = {
    documentElement: root, body,
    querySelector: selector => elements.get(selector.slice(1)) || null,
    querySelectorAll: selector => selector === "button"
      ? [elements.get("approve"), elements.get("cancel")] : [],
  };
  const openai = legacy ? {
    toolOutput: output, toolResponseMetadata: token ? {workspace_confirmation: token} : {},
    callTool: async (name, args) => {
      calls.push({name, args: structuredClone(args)});
      return callTool ? callTool(name, args)
        : result(name.startsWith("confirm") ? approved() : cancelled());
    },
    setWidgetState: value => states.push(structuredClone(value)),
  } : undefined;
  const context = {document, parent, openai, console, Date, Map, Object, JSON,
    setTimeout: (fn, delay) => { if (delay < 1000) fn(); return 1; }, clearTimeout() {},
    requestAnimationFrame: fn => { fn(); return null; }, cancelAnimationFrame() {},
    ResizeObserver: class { observe() {} disconnect() {} }, innerWidth: 390,
    addEventListener: (type, fn) => { (listeners[type] ||= []).push(fn); },
    removeEventListener() {}, structuredClone};
  context.window = context;
  vm.createContext(context);
  vm.runInContext(source, context, {filename: "workspace-access-approval.html"});
  for (let index = 0; index < 4; index += 1) await Promise.resolve();
  const message = async data => {
    for (const fn of listeners.message || []) fn({source: parent, data});
    for (let index = 0; index < 4; index += 1) await Promise.resolve();
  };
  return {elements, calls, posted, states, listeners, context, parent, message};
}

test("renders exact immutable scope fields as text without auto-confirming or exposing capability", async () => {
  const app = await load();
  assert.equal(app.calls.length, 0);
  assert.equal(app.elements.get("project").textContent, "alpha");
  assert.equal(app.elements.get("path").textContent, "/workspace/outside/result.json");
  assert.equal(app.elements.get("kind").textContent, "파일");
  assert.equal(app.elements.get("operations").textContent, "읽기");
  assert.equal(app.elements.get("duration").textContent, "15분");
  assert.equal(app.elements.get("purpose").textContent, "검토한 결과 파일 읽기");
  assert.equal(app.elements.get("expiry").textContent,
    new Date("2099-10-21T12:00:00Z").toLocaleString("ko-KR"));
  assert.equal(app.elements.get("epoch").textContent, "7");
  assert.equal(app.elements.get("digest").textContent, DIGEST);
  assert.equal(app.elements.get("scope").textContent, "현재 MCP 설치");
  assert.equal(app.elements.get("approve").disabled, false);
  assert.doesNotMatch(JSON.stringify([...app.elements.values()].map(item => item.textContent)),
    new RegExp(TOKEN));
  assert.doesNotMatch(html.split("<script", 1)[0], /confirmation|token|capability/i);
});

test("accepts the thirty-day maximum and renders long durations in days", async () => {
  const app = await load({output: pending({duration_seconds: 2592000})});
  assert.equal(app.elements.get("duration").textContent, "30일");
  assert.equal(app.elements.get("approve").disabled, false);
});

test("fast double approval sends one callback and publishes only a bounded receipt", async () => {
  let release;
  const delayed = new Promise(resolve => { release = resolve; });
  const app = await load({callTool: async () => delayed});
  const first = app.elements.get("approve").dispatch("click");
  const second = app.elements.get("approve").dispatch("click");
  assert.equal(app.calls.length, 1);
  release(result(approved()));
  await Promise.all([first, second]);
  assert.deepEqual(app.calls[0], {name: "confirm_workspace_access_approval", args: {
    project: "alpha", request_id: REQUEST, confirmation_token: TOKEN,
  }});
  assert.equal(app.elements.get("approve").disabled, true);
  assert.equal(JSON.stringify(app.states).includes(TOKEN), false);
  assert.equal(JSON.stringify(app.states).includes("confirmation_token"), false);
  assert.deepEqual(app.states[0].modelContent.workspaceAccess, approved());
});

test("mismatched or rejected callback locks the card without replay", async () => {
  for (const callTool of [
    async () => result(approved({request_id: "9".repeat(32)})),
    async () => ({isError: true, structuredContent: {status: "denied"}}),
  ]) {
    const app = await load({callTool});
    await app.elements.get("approve").dispatch("click");
    await app.elements.get("approve").dispatch("click");
    assert.equal(app.calls.length, 1);
    assert.equal(app.elements.get("approve").disabled, true);
    assert.equal(app.elements.get("cancel").disabled, true);
    assert.match(app.elements.get("status").textContent, /다시 열어/);
  }
});

test("expired request never enables actions", async () => {
  const app = await load({output: pending({request_expires_at: "2000-01-01T00:00:00Z"})});
  assert.equal(app.elements.get("approve").disabled, true);
  assert.equal(app.elements.get("cancel").disabled, true);
  assert.match(app.elements.get("status").textContent, /만료/);
  await app.elements.get("approve").dispatch("click");
  assert.equal(app.calls.length, 0);
});

test("cancellation is one-use and produces no approval call", async () => {
  const app = await load();
  await app.elements.get("cancel").dispatch("click");
  await app.elements.get("cancel").dispatch("click");
  assert.equal(app.calls.length, 1);
  assert.equal(app.calls[0].name, "cancel_workspace_access_approval");
  assert.equal(app.elements.get("approve").disabled, true);
  assert.match(app.elements.get("status").textContent, /취소/);
});

test("a terminal card cannot be re-enabled by a replayed pending notification", async () => {
  const app = await load();
  await app.elements.get("approve").dispatch("click");
  assert.equal(app.calls.length, 1);

  await app.message({jsonrpc: "2.0", method: "ui/notifications/tool-result",
    params: result(pending(), TOKEN)});
  assert.equal(app.elements.get("approve").disabled, true);
  assert.equal(app.elements.get("cancel").disabled, true);
  await app.elements.get("approve").dispatch("click");
  assert.equal(app.calls.length, 1);
  assert.match(app.elements.get("status").textContent, /다시 열어/);
});

test("modern MCP Apps RPC initializes, resizes, accepts notification and updates context", async () => {
  const app = await load({legacy: false});
  const initialize = app.posted.find(item => item.method === "ui/initialize");
  assert.ok(initialize);
  await app.message({jsonrpc: "2.0", id: initialize.id,
    result: {hostCapabilities: {}, hostContext: {theme: "dark"}}});
  assert.equal(app.context.document.documentElement.style.colorScheme, "dark");
  await app.message({jsonrpc: "2.0", method: "ui/notifications/host-context-changed",
    params: {theme: "light"}});
  assert.equal(app.context.document.documentElement.style.colorScheme, "light");
  await app.message({jsonrpc: "2.0", method: "ui/notifications/tool-result",
    params: result(pending(), TOKEN)});
  assert.equal(app.elements.get("approve").disabled, false);
  assert.ok(app.posted.some(item => item.method === "ui/notifications/size-changed"));
  const clicked = app.elements.get("approve").dispatch("click");
  await Promise.resolve();
  const call = app.posted.find(item => item.method === "tools/call");
  assert.ok(call);
  await app.message({jsonrpc: "2.0", id: call.id, result: result(approved())});
  await clicked;
  const context = app.posted.find(item => item.method === "ui/update-model-context");
  assert.ok(context);
  assert.equal(JSON.stringify(context).includes(TOKEN), false);
});
