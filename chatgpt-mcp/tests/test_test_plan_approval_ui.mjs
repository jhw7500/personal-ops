/** One-use TEST approval card regressions; synthetic DOM, no host effects. */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const html = fs.readFileSync(new URL("../host-test-plan-approval.html", import.meta.url), "utf8");
const source = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
const OPERATION = "1".repeat(32);
const CARD = "2".repeat(32);
const TOKEN = "3".repeat(64);

test("admin fallback displays the fixed 015/016 DB impact and exact hashes", async () => {
  const admin = fs.readFileSync(new URL("../admin.js", import.meta.url), "utf8");
  const start = admin.indexOf("  async function showHostDetails() {");
  const end = admin.indexOf("  async function refreshHostPlans() {", start);
  assert.ok(start >= 0 && end > start);
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, { textContent: "", replaceChildren() {} });
    return elements.get(id);
  };
  const checksums = {
    "015_public_demo_video_unlocks.sql": "a".repeat(64),
    "016_rewarded_ad_attempts.sql": "b".repeat(64),
  };
  const generation = { database: "tarororo_test", user: "tarororo",
                       postmasterStartEpoch: "1792379543.782441" };
  const plan = {
    component: "schema015016", operation_id: OPERATION, mode: "disabled",
    impact: { recreate: null, preserve: ["api", "worker"],
              schema_change: "015+016_fixed_sources" },
    migration015016_checksums: checksums, database_generation: generation,
  };
  await vm.runInNewContext(`(${admin.slice(start, end)})()`, {
    $: element, selectedHostPlan: () => plan, hostReviewEpoch: 0,
    hostReviewPlan: "", hostReviewReady: false, needsHostReview: () => false,
    activeTargetAuthorizedPlan: () => false, targetAuthorizedPlan: () => false,
    updateHostApproval() {},
  });
  const details = JSON.parse(element("host-details").textContent);
  assert.equal(details.recreate, null);
  assert.equal(details.schema_change, "015+016_fixed_sources");
  assert.match(details.schema_scope, /015.*016/);
  assert.doesNotMatch(details.schema_scope, /DB 스키마 변경 없음/);
  assert.deepEqual(details.migration015016_checksums, checksums);
  assert.deepEqual(details.database_generation, generation);
});

class Element {
  constructor(id = "") {
    this.id = id;
    this.children = [];
    this.listeners = {};
    this.dataset = {};
    this.disabled = false;
    this.style = {};
  }
  get textContent() { return this.children.join(""); }
  set textContent(value) { this.children = [String(value)]; }
  get innerHTML() { throw new Error("innerHTML is forbidden"); }
  set innerHTML(_) { throw new Error("innerHTML is forbidden"); }
  addEventListener(type, cb) { (this.listeners[type] ||= []).push(cb); }
  async dispatch(type) {
    await Promise.all((this.listeners[type] || []).map(fn => fn({ type })));
  }
  getBoundingClientRect() { return { height: 450 }; }
}

const fixture = (overrides = {}) => ({
  project: "tossApp", target: "tarororo-test", operation_id: OPERATION,
  card_id: CARD, component: "api", mode: "toss-demo",
  state: "pending", plan_digest: "4".repeat(64),
  source_digest: "5".repeat(64), expires_at: "2099-10-16T05:30:00+00:00",
  impact: { recreates: ["api"], preserves: ["worker", "db"], schema_change: null },
  high_risk_changes: [], changed_path_count: 0,
  executes_on_approval: false, cancellation_available: true, ...overrides,
});

function load({ output = fixture(), metadata = { test_plan_confirmation: TOKEN },
                callTool } = {}) {
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  const elements = new Map(ids.map(id => [id, new Element(id)]));
  const posted = [];
  const calls = [];
  const document = {
    documentElement: new Element("root"), body: new Element("body"),
    getElementById: id => elements.get(id) || null,
  };
  const context = {
    document, console, Date, Map, Object,
    setTimeout: fn => { fn(); return 1; }, clearTimeout() {},
    requestAnimationFrame: fn => { fn(); return 1; },
    ResizeObserver: class { observe() {} }, innerWidth: 390,
    addEventListener() {},
  };
  context.parent = { postMessage: value => posted.push(value) };
  context.openai = {
    toolOutput: output, toolResponseMetadata: metadata,
    callTool: async (name, args) => {
      calls.push({ name, args });
      return callTool ? callTool(name, args) : {
        structuredContent: fixture({
          state: name.startsWith("cancel") ? "cancelled" : "approved",
          approved: !name.startsWith("cancel"), cancellation_available: false,
        }),
      };
    },
    setWidgetState() {},
  };
  context.window = context;
  vm.createContext(context);
  assert.ok(source, "TEST plan card must contain an inline script");
  vm.runInContext(source, context, { filename: "host-test-plan-approval.html" });
  return { elements, posted, calls, context };
}

test("pending card exposes approval and cancel; no automatic execution", () => {
  const app = load();
  assert.equal(app.elements.get("scope").textContent, "tossApp / tarororo-test");
  assert.equal(app.elements.get("approve").disabled, false);
  assert.equal(app.elements.get("cancel").disabled, false);
  assert.deepEqual(app.calls, []);
  assert.doesNotMatch(html, /innerHTML\s*=/);
});

test("approved but unexecuted API plan offers withdrawal, never reapproval", async () => {
  const app = load({ output: fixture({
    state: "approved", approved: true, receipt_verified: true,
  }) });
  assert.equal(app.elements.get("approve").disabled, true);
  assert.equal(app.elements.get("cancel").disabled, false);
  assert.equal(app.elements.get("cancel").textContent, "미실행 승인 철회");
  await app.elements.get("cancel").dispatch("click");
  assert.equal(app.calls.length, 1);
  assert.equal(app.calls[0].name, "cancel_test_plan_approval");
  assert.deepEqual(JSON.parse(JSON.stringify(app.calls[0].args)), {
    project: "tossApp", target: "tarororo-test",
    operation_id: OPERATION, card_id: CARD, confirmation_token: TOKEN,
  });
  assert.equal(app.elements.get("cancel").disabled, true);
  assert.match(app.elements.get("status").textContent, /철회했습니다/);
  assert.equal(app.calls.some(c => c.name.includes("execute")), false);
});

test("approved card without new secret token is fail-closed", () => {
  const app = load({
    output: fixture({ state: "approved", receipt_verified: true }),
    metadata: {},
  });
  assert.equal(app.elements.get("approve").disabled, true);
  assert.equal(app.elements.get("cancel").disabled, true);
  assert.match(app.elements.get("status").textContent, /확인 정보가 없습니다/);
});

test("outcome-uncertain withdrawal does not retry or mark cancellation", async () => {
  const app = load({
    output: fixture({ state: "approved", receipt_verified: true }),
    callTool: async () => { throw new Error("synthetic callback failure"); },
  });
  await app.elements.get("cancel").dispatch("click");
  assert.equal(app.calls.length, 1);
  assert.equal(app.elements.get("cancel").disabled, true);
  assert.match(app.elements.get("status").textContent, /결과가 불확실/);
});

test("Schema 015+016 migration has its own pending one-use approval", async () => {
  const app = load({ output: fixture({
    component: "schema015016", mode: "disabled",
    impact: { recreates: [], preserves: ["api", "worker"], schema_change: "015+016_fixed_sources" },
  }) });
  assert.match(app.elements.get("component").textContent, /015\+016/);
  assert.equal(app.elements.get("approve").disabled, false);
  await app.elements.get("approve").dispatch("click");
  assert.equal(app.calls.length, 1);
  assert.equal(app.calls[0].name, "confirm_test_plan_approval");
  assert.equal(app.calls.some(c => c.name.includes("execute")), false);
});
