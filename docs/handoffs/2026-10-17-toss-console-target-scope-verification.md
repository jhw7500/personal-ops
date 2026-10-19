# Toss TEST 콘솔 OAuth 준비 및 scope guard 점검 — 2026-10-17

Project: personal-ops (target tossApp / tarororo-test). Status: source-only, official provider OAuth and exact bundle status NOT verified.

## Grounded public facts

- Official Apps in Toss harness: https://github.com/toss/apps-in-toss-harness
- Console MCP: https://mcp.toss.im/adapters/apps-in-toss-console/mcp
- OAuth resource and issuer observed on 2026-10-17: https://mcp.toss.im, with interactive authorization/token endpoints at business-accounts.toss.im.
- Static OAuth client ID: mcp-gateway. DCR is not supported by the published harness.
- Authenticated read-only tools listed in harness: bundle_list, miniapp_get_status. The exact actual tool schema/pagination is NOT available from this repo's unauthenticated session and must be reviewed after a separate owner-approved login.

## Latest source changes

- chatgpt-mcp/toss_console_sdk_bridge.py: ReviewedBundleListContract now requires BOTH fixed workspace ID 93125 and miniapp ID 75257 among the reviewed arguments. It no longer permits miniapp-only or workspace-only queries.
- chatgpt-mcp/toss_console_evidence.py: when provider explicitly includes workspaceId/workspace_id and miniappId/miniapp_id, each identifier must exactly match the fixed target (bool and inconsistent duplicate aliases rejected); missing optional scope metadata is not invented.
- chatgpt-mcp/tests/test_toss_console_sdk_bridge.py, tests/test_toss_console_evidence.py: added negative tests for missing workspace scope and inconsistent provider scope and a positive case for matching scope metadata.
- First isolated test job f6a7da4a6a62444bb2c4a78554035bfd: 69 tests, one ERROR because an existing mock schema-name rejection test failed at the earlier, intentionally stricter constructor gate; test expectation updated.
- Final isolated job ca0ca04799ce4a078e6207e78c0c9f2a: 69/69 PASS, exit code 0, with evidence, SDK bridge, façade, runtime-install and rollout-source test modules.
- This is fail-closed until the **actual** bundle_list tool inputSchema is obtained. If Toss does not offer both workspace and miniapp identifiers as tool arguments, the secure design must instead establish workspace boundary through a trusted separately verified session/account and adjust the reviewed contract; do NOT guess field names or broaden the tool automatically.

## Current security boundaries

- The source server still initializes TossConsoleReadOnlyTools(provider=None), so get_toss_test_console_status stays blocked with CONSOLE_OAUTH_NOT_CONFIGURED.
- ReviewedMcpBundleListProvider(trusted_oauth_session=True) is a test/integration assertion and DOES NOT prove OAuth. Before any runtime activation, the owner-reviewed secure session factory must attest TLS, user consent, intended issuer/resource, intended workspace, and actual verified schema. Do not instantiate this from a chat payload.
- The public harness lists console tools that can modify or upload bundles. The adapter permits ONLY bundle_list after catalog validation. No generic OAuth token exposure, upload, review, release, or arbitrary tool proxy.
- The latest prior complete official preflight PASS (target snapshot 7ed51c90eb1a34a631a6) was run BEFORE this and the prior SDK bridge source edits, so it must be repeated for any future proposed runtime source snapshot.
- No OAuth login, remote status lookup, host runtime install/restart, Docker/API/Worker/DB write, AIT upload or Git commit/stage/reset was done in this turn.

## The actual next action requiring owner involvement

Route A — verify the existing uploaded TEST bundle through the official Toss Console UI, without any new AIT upload: https://apps-in-toss.toss.im/workspace/93125/mini-app/75257/app-build. In the logged-in browser, find exact deployment ID 01a13e37-27f3-7f9c-923d-6dbd690a5a09 and record the real build/review status, isTested, deployed, and version. A screenshot or authenticated record can be used to continue device QA, but a user-supplied image is not the same as ChatGPT independently calling the provider.
Route B — permanent personal-ops connector: user must authorize official MCP in a supported interactive OAuth client. Public harness supports apps-in-toss-console authorization in the host client interface; the authorization belongs to THAT client and may not be copied from Claude/Codex storage into personal-ops. Implement a separate owner-controlled authenticated connection/session factory and validate actual tool schema before integrating this module. A new, separately approved immutable rollout then follows.

Exact test artifact: SHA256 d14d4a67fad63cf7aa623e5c1bc385a7a356add6a73620d3dfe4fead1380305f; Deployment ID 01a13e37-27f3-7f9c-923d-6dbd690a5a09. CLI and local receipt succeeded, remote digest not verified. No repeat upload or UUID mutation.
