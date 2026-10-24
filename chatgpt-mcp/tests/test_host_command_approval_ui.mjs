import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const html = fs.readFileSync(new URL("../host-command-approval.html", import.meta.url), "utf8");
const source = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
const OPERATION = "1".repeat(32);
const CARD = "2".repeat(32);
const TOKEN = "3".repeat(64);
const PLAN = "4".repeat(64);
const SOURCE = "5".repeat(64);
const EXECUTABLE = "6".repeat(64);
const XSS = '<img src=x onerror="globalThis.pwned=1">';

class Element {
  constructor(id = "") {
    this.id = id; this.children = []; this.listeners = {}; this.dataset = {};
    this.disabled = false; this.style = {};
  }
  get textContent() { return this.children.join(""); }
  set textContent(value) { this.children = [String(value)]; }
  get innerHTML() { throw new Error("innerHTML is forbidden"); }
  set innerHTML(_) { throw new Error("innerHTML is forbidden"); }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  async dispatch(type) { await Promise.all((this.listeners[type] || []).map(fn => fn({type}))); }
  getBoundingClientRect() { return {height: 520}; }
}

const fixture = (overrides = {}) => {
  const base = {
    project: "example", component: "host_command", profile_id: "test-upload",
    label: `TEST bundle upload ${XSS}`, effect: "external_publish",
    operation_id: OPERATION, card_id: CARD, state: "pending",
    plan_digest: PLAN, source_digest: SOURCE, executable_sha256: EXECUTABLE,
    input_count: 1, timeout_seconds: 60,
    expires_at: "2099-10-16T05:30:00+00:00", executes_on_approval: false,
    receipt_verified: false,
  };
  const value = {...base, ...overrides};
  value.plan = overrides.plan ?? {
    project: value.project, profile_id: value.profile_id, label: value.label,
    effect: value.effect, input_count: value.input_count,
    timeout_seconds: value.timeout_seconds, executable_sha256: value.executable_sha256,
  };
  return value;
};

const approved = () => fixture({
  state: "approved", receipt_verified: true, approved: true,
  next_action: {tool: "execute_host_command", arguments: {
    project: "example", operation_id: OPERATION,
  }},
});

const directPending = () => fixture({supports_direct_execution: true});

const executing = (overrides = {}) => fixture({
  supports_direct_execution: false, state: "approved", receipt_verified: true, approved: true,
  execution_requested: true, execution_started: true,
  plan: {state: "running", phase: "execute", code: null,
    started_at: "2026-10-20T01:02:03Z"},
  next_action: {tool: "get_host_command", arguments: {
    project: "example", operation_id: OPERATION,
  }},
  ...overrides,
});

function result(value, token = undefined) {
  return {content: [{type: "text", text: "bounded"}], structuredContent: value,
    ...(token ? {_meta: {host_command_confirmation: token}} : {})};
}

async function load({output = fixture(), token = TOKEN, callTool} = {}) {
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  const elements = new Map(ids.map(id => [id, new Element(id)]));
  const root = new Element("root");
  const body = new Element("body");
  const posted = [];
  const calls = [];
  const states = [];
  const listeners = {};
  const parent = {postMessage: value => posted.push(value)};
  const document = {documentElement: root, body, getElementById: id => elements.get(id) || null};
  const openai = {
    toolOutput: output, toolResponseMetadata: token ? {host_command_confirmation: token} : {},
    callTool: async (name, args) => {
      calls.push({name, args: structuredClone(args)});
      return callTool ? callTool(name, args) : result(name.startsWith("confirm") ? approved() : fixture({
        state: "cancelled", receipt_verified: false, approved: false,
      }));
    },
    setWidgetState: value => states.push(structuredClone(value)),
  };
  const context = {document, parent, openai, console, Date, Map, Object,
    setTimeout: (fn, delay) => { if (delay <= 15000) fn(); return 1; }, clearTimeout() {},
    requestAnimationFrame: fn => { fn(); return null; }, cancelAnimationFrame() {},
    ResizeObserver: class { observe() {} disconnect() {} }, innerWidth: 390,
    addEventListener: (type, fn) => { (listeners[type] ||= []).push(fn); },
    removeEventListener() {}, structuredClone,
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(source, context, {filename: "host-command-approval.html"});
  for (let index = 0; index < 4; index += 1) await Promise.resolve();
  return {elements, posted, calls, states, listeners, context, parent};
}

test("renders the bounded immutable command plan as text and enables one-use approval", async () => {
  const markup = html.split("<script>", 1)[0];
  assert.match(html, /승인만으로.*실행.*않/);
  assert.match(html, /TEST 업로드/);
  assert.match(html, /외부 게시/);
  assert.doesNotMatch(markup, /관리자 토큰|approval_url|<input|<a\b/i);
  assert.doesNotMatch(html, /innerHTML\s*=/);
  const app = await load();
  assert.equal(app.elements.get("project").textContent, "example");
  assert.equal(app.elements.get("profile").textContent, "test-upload");
  assert.equal(app.elements.get("label").textContent, `TEST bundle upload ${XSS}`);
  assert.equal(app.elements.get("effect").textContent, "TEST 업로드 · 외부 게시");
  assert.equal(app.elements.get("operation").textContent, OPERATION);
  assert.equal(app.elements.get("plan").textContent, PLAN);
  assert.equal(app.elements.get("source").textContent, SOURCE);
  assert.equal(app.elements.get("executable").textContent, EXECUTABLE);
  assert.equal(app.elements.get("inputs").textContent, "1개");
  assert.equal(app.elements.get("timeout").textContent, "60초");
  assert.doesNotMatch(app.elements.get("expiry").textContent, /불러오는 중/);
  assert.equal(app.context.pwned, undefined);
  assert.equal(app.elements.get("approve").disabled, false);
  assert.ok(app.posted.some(item => item.method === "ui/notifications/size-changed"));
  assert.doesNotMatch(app.elements.get("status").textContent, new RegExp(TOKEN));
});

test("one click sends only the hidden approval callback and never executes the command", async () => {
  const app = await load();
  await app.elements.get("approve").dispatch("click");
  assert.deepEqual(app.calls, [{name: "confirm_host_command_approval", args: {
    project: "example", operation_id: OPERATION, card_id: CARD,
    confirmation_token: TOKEN,
  }}]);
  assert.equal(app.calls.some(call => call.name.includes("execute")), false);
  assert.equal(app.elements.get("approve").disabled, true);
  assert.match(app.elements.get("status").textContent, /승인 영수증/);
  assert.match(app.elements.get("status").textContent, /실행.*별도/);
  assert.notEqual(JSON.stringify(app.states).includes(TOKEN), true);
  assert.notEqual(JSON.stringify(app.states).includes("next_action"), true);
});

test("direct execution card clearly starts publish work with one combined callback", async () => {
  const app = await load({output: directPending(), callTool: async () => result(executing())});
  assert.equal(app.elements.get("approve").textContent, "승인하고 실행");
  assert.match(app.elements.get("warning-title").textContent, /승인하면.*실행/);
  assert.match(app.elements.get("warning-detail").textContent, /TEST 업로드.*외부 게시/);
  assert.doesNotMatch(app.elements.get("warning-title").textContent, /실행하지 않습니다/);
  await app.elements.get("approve").dispatch("click");
  assert.deepEqual(app.calls, [{name: "confirm_and_execute_host_command", args: {
    project: "example", operation_id: OPERATION, card_id: CARD,
    confirmation_token: TOKEN,
  }}]);
  assert.match(app.elements.get("status").textContent, /실행을 시작/);
  assert.doesNotMatch(app.elements.get("status").textContent, /별도.*요청/);
  assert.equal(app.calls.some(call => call.name === "execute_host_command"), false);
  assert.notEqual(JSON.stringify(app.states).includes(TOKEN), true);
  assert.notEqual(JSON.stringify(app.states).includes("supports_direct_execution"), true);
  assert.deepEqual(app.states[0].modelContent.hostCommandApproval.execution_requested, true);
  assert.deepEqual(app.states[0].modelContent.hostCommandApproval.execution_started, true);
  assert.equal(app.states[0].modelContent.hostCommandApproval.plan.state, "running");
  assert.notEqual(JSON.stringify(app.states).includes("next_action"), true);
});

test("direct execution expiration is terminal and never retries", async () => {
  const stopped = executing({execution_started: false,
    plan: {state: "expired", phase: "stopped", code: "plan_expired", started_at: null}});
  const app = await load({output: directPending(), callTool: async () => result(stopped)});
  await app.elements.get("approve").dispatch("click");
  assert.equal(app.calls.length, 1);
  assert.match(app.elements.get("status").textContent, /만료.*실행.*시작하지/);
  assert.equal(app.elements.get("approve").disabled, true);
  assert.equal(app.elements.get("cancel").disabled, true);
});

test("a fast double click cannot issue two approval callbacks", async () => {
  let release;
  const delayed = new Promise(resolve => { release = resolve; });
  const app = await load({callTool: async () => delayed});
  const first = app.elements.get("approve").dispatch("click");
  const second = app.elements.get("approve").dispatch("click");
  assert.equal(app.calls.length, 1);
  release(result(approved()));
  await Promise.all([first, second]);
  assert.equal(app.calls.length, 1);
});

test("a fast double click on direct execution issues one combined callback", async () => {
  let release;
  const delayed = new Promise(resolve => { release = resolve; });
  const app = await load({output: directPending(), callTool: async () => delayed});
  const first = app.elements.get("approve").dispatch("click");
  const second = app.elements.get("approve").dispatch("click");
  assert.equal(app.calls.length, 1);
  assert.equal(app.calls[0].name, "confirm_and_execute_host_command");
  release(result(executing()));
  await Promise.all([first, second]);
  assert.equal(app.calls.length, 1);
});

test("cancel shows the true approved state when approval won the race", async () => {
  const app = await load({callTool: async () => result(approved())});
  await app.elements.get("cancel").dispatch("click");
  assert.equal(app.calls[0].name, "cancel_host_command_approval");
  assert.match(app.elements.get("status").textContent, /이미 승인/);
  assert.doesNotMatch(app.elements.get("status").textContent, /취소했습니다/);
  assert.equal(app.elements.get("approve").disabled, true);
  assert.equal(app.elements.get("cancel").disabled, true);
});

test("cancel invokes only the cancellation callback and renders the terminal state", async () => {
  const app = await load();
  await app.elements.get("cancel").dispatch("click");
  assert.equal(app.calls.length, 1);
  assert.equal(app.calls[0].name, "cancel_host_command_approval");
  assert.equal(app.calls.some(call => call.name.includes("execute")), false);
  assert.match(app.elements.get("status").textContent, /취소했습니다/);
  assert.equal(app.elements.get("approve").disabled, true);
  assert.equal(app.elements.get("cancel").disabled, true);
});

test("ambiguous callback and cross-plan response lock both actions without retry", async () => {
  for (const callTool of [
    async () => { throw new Error("synthetic response loss"); },
    async () => result({...approved(), operation_id: "9".repeat(32)}),
    async () => result({...approved(), plan_digest: "9".repeat(64)}),
  ]) {
    const app = await load({callTool});
    await app.elements.get("approve").dispatch("click");
    assert.equal(app.calls.length, 1);
    assert.equal(app.elements.get("approve").disabled, true);
    assert.equal(app.elements.get("cancel").disabled, true);
    assert.match(app.elements.get("status").textContent, /결과를 확정할 수 없습니다/);
  }
});

test("approved reopen is terminal and cannot approve again", async () => {
  const app = await load({output: approved(), token: null});
  assert.equal(app.elements.get("approve").disabled, true);
  assert.equal(app.elements.get("cancel").disabled, true);
  assert.match(app.elements.get("status").textContent, /승인됨/);
  await app.elements.get("approve").dispatch("click");
  assert.equal(app.calls.length, 0);
});

test("direct approved reopen is read-only and does not need a token", async () => {
  const app = await load({output: executing(), token: null});
  assert.equal(app.elements.get("approve").disabled, true);
  assert.equal(app.elements.get("cancel").disabled, true);
  assert.match(app.elements.get("status").textContent, /실행.*시작/);
  assert.equal(app.calls.length, 0);
});

test("expired, missing-token, malformed, and pending next-action cards fail closed", async () => {
  const cases = [
    {output: fixture({expires_at: "2000-01-01T00:00:00Z"}), token: TOKEN},
    {output: fixture(), token: null},
    {output: fixture({profile_id: "Bad Profile"}), token: TOKEN},
    {output: fixture({next_action: {tool: "execute_host_command", arguments: {
      project: "example", operation_id: OPERATION,
    }}}), token: TOKEN},
    {output: fixture({plan: {project: "example", argv: ["secret"]}}), token: TOKEN},
    {output: fixture({plan: {project: "example", cwd: "/private/host/path"}}), token: TOKEN},
    {output: directPending(), token: TOKEN, callTool: async () => result(executing({
      next_action: {tool: "execute_host_command", arguments: {
        project: "example", operation_id: OPERATION,
      }},
    }))},
  ];
  for (const item of cases) {
    const app = await load(item);
    if (item.callTool) await app.elements.get("approve").dispatch("click");
    assert.equal(app.elements.get("approve").disabled, true);
    assert.equal(app.elements.get("cancel").disabled, true);
    assert.equal(app.calls.length, item.callTool ? 1 : 0);
  }
});

test("messages from a non-parent source are ignored", async () => {
  const app = await load();
  const before = app.elements.get("status").textContent;
  const hostile = result({...fixture(), operation_id: "9".repeat(32)}, "8".repeat(64));
  for (const listener of app.listeners.message || []) {
    listener({source: {}, data: {jsonrpc: "2.0", method: "ui/notifications/tool-result",
      params: hostile}});
  }
  assert.equal(app.elements.get("status").textContent, before);
  assert.equal(app.elements.get("approve").disabled, false);
});
