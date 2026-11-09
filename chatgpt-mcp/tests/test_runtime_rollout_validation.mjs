import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import {fileURLToPath} from "node:url";
import path from "node:path";
import test from "node:test";

const dirname = path.dirname(fileURLToPath(import.meta.url));
const html = fs.readFileSync(path.resolve(dirname, "../runtime-rollout-approval.html"), "utf8");
const start = html.indexOf("    function safeText(");
const end = html.indexOf("    function terminal(");
const definitionsStart = html.indexOf("    const RESTART_BY_LAYOUT =");
const definitionsEnd = html.indexOf("    const STATES =");
assert.ok(start >= 0 && end > start, "approval validator must exist");
assert.ok(definitionsStart >= 0 && definitionsEnd > definitionsStart, "layouts must be validated");

const sandbox = {
  HEX32: /^[0-9a-f]{32}$/,
  HEX64: /^[0-9a-f]{64}$/,
  SNAPSHOT: /^[0-9a-f]{20}$/,
  STATES: new Set(["pending", "approved", "cancelled"]),
  FORBIDDEN_PLAN_KEYS: new Set([
    "argv", "cwd", "cwd_relative", "executable", "approval_url",
    "approval_command", "confirmation_token", "runtime_rollout_confirmation",
    "nonce", "environment", "env",
  ]),
};
vm.runInNewContext(
  html.slice(definitionsStart, definitionsEnd) + html.slice(start, end) + "\nthis.validate = validPublic;", sandbox,
);

function fixture(target_layout, restart) {
  const project = "personal-ops";
  const operation_id = "1".repeat(32);
  const plan_digest = "2".repeat(64);
  const source_digest = "3".repeat(64);
  const control_digest = "4".repeat(64);
  const target_snapshot = "5".repeat(20);
  const expires_at = "2099-01-01T00:00:00Z";
  const impact = {
    restart,
    preserve: ["API", "Worker", "PostgreSQL", "Toss TEST bundle"],
  };
  const plan = {
    project, operation_id, plan_digest, source_digest, control_digest,
    target_snapshot, expires_at, target_layout, impact,
  };
  return {
    ...plan, component: "runtime_rollout", card_id: "6".repeat(32),
    state: "pending", receipt_verified: false, executes_on_approval: false,
    source_files: 288, plan,
  };
}

test("legacy rollout's exact service list is accepted", () => {
  assert.equal(sandbox.validate(fixture("legacy", [
    "personal-ops-tunnel.service", "personal-ops-admin.service",
  ])), true);
});

test("WSL rollout's exact service list is accepted", () => {
  assert.equal(sandbox.validate(fixture("wsl-pc1", [
    "personal-ops-coding-wsl-pc1.service", "personal-ops-admin.service",
  ])), true);
});

test("mixed legacy layout and WSL services are rejected", () => {
  assert.equal(sandbox.validate(fixture("legacy", [
    "personal-ops-coding-wsl-pc1.service", "personal-ops-admin.service",
  ])), false);
});

test("unknown services and unknown layouts are rejected", () => {
  assert.equal(sandbox.validate(fixture("wsl-pc1", [
    "unregistered.service", "personal-ops-admin.service",
  ])), false);
  assert.equal(sandbox.validate(fixture("arbitrary", [
    "personal-ops-coding-wsl-pc1.service", "personal-ops-admin.service",
  ])), false);
});
