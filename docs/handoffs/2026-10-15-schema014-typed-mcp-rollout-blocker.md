# Schema 014 native MCP tool rollout — verified source, blocked host preflight

Date: 2026-10-15 KST
Scope: `personal-ops/chatgpt-mcp` to `tossApp/tarororo-test`.
Status: **source integration and relevant unit tests done; privileged installed runtime NOT changed; live Schema 014 NOT executed**.

## 1. User authorization
User explicitly requests full TEST deployment: Schema014 DB-only → API-only with `toss-demo` → exact AIT TEST upload. Only registered and owner-approved paths may execute DB writes or production-side effects; no direct SQL, manual Docker exec, broad host shell replacement or skipping one-use approvals.

## 2. Implemented source changes
- New `chatgpt-mcp/host_schema014.py`: fixed migration `014_person_relationship_detail.sql`, SHA `a7b470a05007543be8751aec6d2ea8cd88891b08aa736bc72112b7417d56dac2`; 001–013 exact history read-only preflight, DB generation/column/constraint/aggregate count inspection; backup, archive validation, disposable restore, same-generation guarded atomic DDL, checked result. No arbitrary SQL accepted, no API/Worker/DB restart.
- `chatgpt-mcp/host_db_migration.py` restore verification accepts 13-source history.
- `host_runtime_tools.py` exposes exact-scope `get_schema014_migration_status`, `prepare_schema014_migration`, `execute_schema014_migration`. Existing 013 tools remain separately scoped.
- `host_runtime_operations.py`: `schema014` typed component on registered reviewed_snapshot only, always `{mode:per_operation}` (historical target authorization **cannot** authorize it), immutable frozen source, exact SQL, pinned generation, 10-min plan, one-use approved receipt, backup/restore execution and public postcheck state.
- `server.py` includes exact typed routing and same project lock for preparation; `host_runtime_web.py` includes approval page metadata. `install-runtime.py` + runtime verified manifest add the new module.
- `rollout-host-runtime.py` TESTS and TOOL_NAMES include Schema014 and abort future switching unless the exact three tools are advertised.
- Tests `test_host_schema014.py` (9), `test_host_schema014_approval.py` (4) plus legacy `test_host_runtime.py`, `test_rollout_host_runtime.py`, `test_host_simplified_approval.py` expectation changes.
- Corrected two preexisting test fixtures for host `umask=077`: `tests/test_agent_bootstrap.py` and `tests/test_agent_code_mode.py` explicitly chmod their intentionally unsafe parent to 0755. Did NOT relax agent implementation safety checks.

## 3. Verified checks
- Isolated fixed-SHA 014 tests 9/9 PASS, separate-approval/exact tool tests 4/4 PASS, backup 13/13 PASS, previous 006–013 migration tests 16/16 and target authorization 7/7 PASS, Python syntax PASS.
- Installer snapshot tests 9/9 PASS; rollout preflight code tests 13/13 PASS; rollout catalog 5/5 PASS; host runtime tests 31/31 and simple-approval tests pass following expected component-list update.
- Official actual host `rollout-host-runtime.py` preflight #1 (host op `27c04e97b7f66cd7b5455bca0a9b51e7`) **FAILED** targeted 3 tests, 1 new component-list expectation and 2 host umask-dependent test fixtures; corrected and separately retested those 3/3 PASS.
- Official host preflight #2 (host op `ddc3a386ba02e9c5ff1fd5634ad60e70`): **targeted tests PASS** (97.5s), then **full suite FAILED** (125.4s) on 2 legacy integration cases:
  - `tests.test_mcp.CodingMCPTest.test_file_editing_over_stdio`
  - `tests.test_router.MultiProjectRouterTest.test_routes_each_call_and_contains_paths`
  Evidence: owner-private receipt `/home/jhw/.local/state/chatgpt-coding-mcp/rollouts/rollout-hzye95mt/receipt.json`.
- Reproduced exact failures under the correctly invoked Serena tool venv through a permitted read-only host test (op `87ab88e160d145f1576616d167effde7`): in temporary test projects Serena Python language server initialization terminated unexpectedly; the `replace_content` request remained unchanged; both assertions failed. This is a real file-editing backend initialization problem, not a Schema014 DDL or type-approval failure.
- Host runtime did **not** install/switch because official preflight exits nonzero. Do NOT delete or skip those tests, invent PASS, or bypass the official rollout gate.

## 4. Outstanding gates
1. Investigate and repair the Serena Python language-server initialization failure in temporary projects; maintain actual `replace_content` capability and project isolation. Preserve existing host client sessions. Repeat the two E2Es under the correct Serena venv.
2. Run the unchanged official host preflight until BOTH targeted and full test stages PASS, then inspect source/control inventory stability.
3. Official `rollout-host-runtime.py --apply` requires an **interactive owner TTY typing ROLLOUT**. The host tool has no PTY; do not fake stdin/PTY to circumvent this check. The owner must perform this specific terminal confirmation in the supported CLI. New release installation is not evidence of active tunnel switch. Verify actual tunnel tool discovery and admin readiness after switch.
4. Only then create `prepare_schema014_migration` plan from active installed MCP, have owner separately approve its exact operation/digest through authenticated management web, `execute_schema014_migration`, and confirm `get_schema014_migration_status=ready`. The 001–013 target grant does NOT authorize 014.
5. Plan API-only deploy with mode toss-demo; execute only after 014 is confirmed, verify Worker/DB images+start generations unchanged; then exact SHA-pin and upload TEST AIT `tossApp/build/android-test/gone-ux-navigation-relationship014-20261015/tarororo.ait` sha256 `d14d4a67fad63cf7aa623e5c1bc385a7a356add6a73620d3dfe4fead1380305f`.
6. Toss WebView real device QA (native back/forward, relationship label and separate situation persistence, 5s/5s/30s toss-demo sample ads). Not a production/public release.



## Follow-up — root cause fixed and official preflight PASS (2026-10-15 ~17:04 KST)
- Identified why the two legacy Serena E2Es failed: host restricted command PATH selected `/usr/bin/node` **v12.22.9**, and `python -m pyright --version` failed. Existing owner-installed Node `~/.nvm/versions/node/v24.12.0/bin/node` runs v24.12.0. Under PATH with Node24 first, the exact two unchanged MCP editing/router E2Es **2/2 PASS**, host operation `632982aa826f741dbfd690aefd1e0207`.
- First official Node24 preflight operation `8867f9ebd35cb89fe1a0ff6099dd2f3f`: targeted **621/621 PASS**, full **786/786 PASS**, tunnel status lookup succeeded, but `systemctl --user cat` failed because the restricted host launcher did not export `XDG_RUNTIME_DIR` or `DBUS_SESSION_BUS_ADDRESS`. Direct read-only `systemctl --user show` with `XDG_RUNTIME_DIR=/run/user/1003` and `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1003/bus` succeeded. No program logic/authorization tests were suppressed.
- Final official preflight `d598cab9893f9697eb56f70d1110d1d2`, with Node24 PATH + fixed user bus environment: **exit0, preflight_ok**, source_files=71, tunnel/profile/service aligned. New immutable runtime snapshot predicted `56970e7775b936d00284`, currently active snapshot `1bf5d7d05cc5ca07d8e1`. Crucially **installed=false, switched=false**; no Docker, migration or CI deployment.
- Retained owner-private preflight receipt `/home/jhw/.local/state/chatgpt-coding-mcp/rollouts/rollout-2pfy_80d/receipt.json`.
- The official `--apply` path requires a **real owner-controlled interactive TTY** and typing `ROLLOUT` after checks. Host-execution MCP lacks a TTY and must NOT fake or emulate one. Operator command on the host interactive terminal:
```bash
cd ~/ai/opencode/projects/personal-ops
PATH="$HOME/.nvm/versions/node/v24.12.0/bin:/usr/local/bin:/usr/bin:/bin" \
XDG_RUNTIME_DIR="/run/user/$(id -u)" \
DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus" \
"$HOME/.local/share/uv/tools/serena-agent/bin/python" \
chatgpt-mcp/rollout-host-runtime.py --apply
```
- After operator's TTY approval, user refreshes ChatGPT plugin and validates the three new exact-scope Schema014 tools. Then prepare migration plan, owner reviews/approves exact one-use plan in management web, and only then execute DB-only 014 → API-only toss-demo → exact AIT TEST upload. Do not reuse old 013 target grant. This session has not executed these effects.

## 5. Non-effects
No Git reset, stash, commit, push or source history rewrite; no agent calls; no DB 014 or API/Worker Docker deploy; no actual AIT upload. Last verified dedicated test DB remained schema013 Ready, API Healthy/Ready/toss-demo, Worker/DB Running. Never treat successful source-unit tests as permission to skip a failed host preflight.
