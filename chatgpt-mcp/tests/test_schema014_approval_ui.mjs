import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const html = fs.readFileSync(new URL("../host-operation-approval.html", import.meta.url), "utf8");
const source = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
const OPERATION = "1".repeat(32);
const CARD = "2".repeat(32);
const TOKEN = "3".repeat(64);

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
  async dispatch(type) { await Promise.all((this.listeners[type] || []).map(fn => fn({ type }))); }
  getBoundingClientRect() { return { height: 480 }; }
}

const fixture = (overrides = {}) => ({
  project: "tossApp", target: "tarororo-test", component: "schema014",
  operation_id: OPERATION, card_id: CARD, state: "pending",
  plan_digest: "4".repeat(64), source_digest: "5".repeat(64),
  schema014_sha256: "6".repeat(64),
  predecessor_checksums: { "001.sql": "7".repeat(64) },
  database_generation: { database: "tarororo_test" },
  expires_at: "2099-10-16T05:30:00+00:00",
  schema_change: "014_person_relationship_detail.sql",
  backup_before_change: true, service_restart: false,
  executes_on_approval: false, ...overrides,
});

function load({ output = fixture(), callTool } = {}) {
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  const elements = new Map(ids.map(id => [id, new Element(id)]));
  const root = new Element("root");
  const body = new Element("body");
  const posted = [];
  const calls = [];
  const windowListeners = {};
  const document = { documentElement: root, body,
    getElementById: id => elements.get(id) || null };
  const parent = { postMessage: value => posted.push(value) };
  const openai = {
    toolOutput: output, toolResponseMetadata: { schema014_confirmation: TOKEN },
    callTool: async (name, args) => {
      calls.push({ name, args });
      return callTool ? callTool(name, args) : { structuredContent: fixture({
        state: name.startsWith("confirm") ? "approved" : "cancelled",
        approved: name.startsWith("confirm"),
      }) };
    },
    setWidgetState() {},
  };
  const context = { document, parent, openai, console, Date, Map, Object,
    setTimeout: fn => { fn(); return 1; }, clearTimeout() {},
    requestAnimationFrame: fn => { fn(); return 1; }, cancelAnimationFrame() {},
    ResizeObserver: class { observe() {} }, innerWidth: 390,
    addEventListener: (type, fn) => { (windowListeners[type] ||= []).push(fn); },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(source, context, { filename: "host-operation-approval.html" });
  return { elements, posted, calls, context };
}

test("renders exact immutable plan as text, reports height, and enables approval", () => {
  assert.match(html, /Schema 014/);
  assert.match(html, /Backup before change/);
  assert.match(html, /No service restart/);
  assert.doesNotMatch(html, /innerHTML\s*=/);
  const app = load();
  assert.equal(app.elements.get("scope").textContent, "tossApp / tarororo-test");
  assert.equal(app.elements.get("change").textContent, "014_person_relationship_detail.sql");
  assert.equal(app.context.pwned, undefined);
  assert.equal(app.elements.get("operation").textContent, OPERATION);
  assert.equal(app.elements.get("approve").disabled, false);
  assert.ok(app.posted.some(item => item.method === "ui/notifications/size-changed"));
});

test("one click sends only the hidden callback and never starts execution", async () => {
  const app = load();
  await app.elements.get("approve").dispatch("click");
  assert.equal(app.calls.length, 1);
  assert.equal(app.calls[0].name, "confirm_schema014_approval");
  assert.deepEqual(JSON.parse(JSON.stringify(app.calls[0].args)), {
    project: "tossApp", target: "tarororo-test", operation_id: OPERATION,
    card_id: CARD, confirmation_token: TOKEN,
  });
  assert.equal(app.calls.some(call => call.name.includes("execute")), false);
  assert.equal(app.elements.get("approve").disabled, true);
  assert.match(app.elements.get("status").textContent, /승인을 확인/);
  assert.doesNotMatch(app.elements.get("status").textContent, /아직 시작되지 않았/);
});

test("uncertain callback locks both actions instead of retrying", async () => {
  const app = load({ callTool: async () => { throw new Error("synthetic response loss"); } });
  await app.elements.get("approve").dispatch("click");
  assert.equal(app.calls.length, 1);
  assert.equal(app.elements.get("approve").disabled, true);
  assert.equal(app.elements.get("cancel").disabled, true);
  assert.match(app.elements.get("status").textContent, /결과를 확정할 수 없습니다/);
});

test("cancel reports an approval that won the race instead of claiming cancellation", async () => {
  const app = load({ callTool: async () => ({ structuredContent: fixture({
    state: "approved", approved: true, receipt_verified: true,
  }) }) });
  await app.elements.get("cancel").dispatch("click");
  assert.equal(app.calls.length, 1);
  assert.equal(app.calls[0].name, "cancel_schema014_approval");
  assert.match(app.elements.get("status").textContent, /이미 승인/);
  assert.doesNotMatch(app.elements.get("status").textContent, /취소했습니다/);
});
