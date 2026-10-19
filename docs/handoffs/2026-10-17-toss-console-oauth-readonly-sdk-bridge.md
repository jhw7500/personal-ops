# Toss TEST Console OAuth – read-only SDK bridge (source only)

Date: 2026-10-17 (Asia/Seoul). Repository: personal-ops. Target: tossApp / tarororo-test.

## Status

New source-level SDK bridge and regression tests are complete. The actual Apps in Toss OAuth login, authenticated provider query, deployment status check, and production MCP rollout are NOT complete. Existing server.py still constructs TossConsoleReadOnlyTools(provider=None), so the currently connected tool does not perform remote provider calls.

## Official integration boundary

- Console MCP endpoint: https://mcp.toss.im/adapters/apps-in-toss-console/mcp
- OAuth protected resource and issuer: https://mcp.toss.im; authorization/token host is business-accounts.toss.im, using a separately consented user OAuth flow.
- Static client id: mcp-gateway. The official harness notes that dynamic client registration is unsupported.
- Only bundle_list is called by this implementation, never upload, invitation, review, release, DB, Worker, or payment tools.
- Actual provider inputSchema, result format, and pagination must be obtained from a real authenticated session BEFORE connecting this code; the present parser format is only an unverified hypothesis.

## Changed source

- chatgpt-mcp/toss_console_sdk_bridge.py: owner-reviewed bundle_list schema SHA256 and arguments, pinned origin/resource/client ID, missing OAuth and invalid scope fail closed, one read-only tool allowlist, strict catalog schema and readOnly hint validation, 10-second tools/list timeout, 128 KiB result bound, no unreviewed pagination, safe response handling across SDK camelCase and snake_case fields. Arguments are snapshotted against mutation.
- chatgpt-mcp/tests/test_toss_console_sdk_bridge.py: mock session contract, schema pin, scope, errors, timeout, pagination, SDK field spellings, no raw secrets.
- chatgpt-mcp/install-runtime.py and host_runtime_operations.py: include source bridge in future immutable package and verified manifest. No installation was performed.
- chatgpt-mcp/rollout-host-runtime.py and tests/test_runtime_install.py: include new adapter tests in future preflight and package checks.

## Test evidence

- Initial isolated job 3b2161d557204bfbb4ef7c11c6b80773: 38 tests, one failure from an unhandled SDK snake_case next_cursor; fixed.
- Intermediate job 949a4584d1394831b7e60b5dbedd990a: 38/38 PASS.
- Intermediate job e52b7e3e6fab4c208858c4a4cd55782c: 66/66 PASS.
- Final isolated job 76ec347a3c6a44178f5a3f9bf3c3dcc2: 67/67 PASS, exit0, 2.667 seconds.
- These are fake-session tests, NOT an authenticated Apps in Toss provider or live native WebView test.

## Remaining gates

1. User-controlled OAuth approval to the official MCP, without borrowing Claude/Codex credentials or auto-login. ChatGPT has no configured authenticated console MCP connection in this session.
2. Independently verify authenticated session transport, TLS, resource audience and issuer, user workspace permission, actual bundle_list and miniapp_get_status tool schemas, and pagination. A trusted_oauth_session boolean alone is NOT authentication proof.
3. Review and pin exact tool schema hash and call arguments. Attach secure owner-authenticated MCP SDK session factory to the server; no generic tool proxy or user/model-provided authentication values.
4. Re-run full official preflight on new source (previous preflight snapshot 7ed51c90eb1a34a631a6 is stale) before any independently approved immutable rollout, preserving other agents' WIP.
5. Independently verify exact deployment ID 01a13e37-27f3-7f9c-923d-6dbd690a5a09 in the official TEST console, then real Toss WebView QA; never blind-reupload.

## Explicitly not performed

No OAuth login, no token/secret access, no Apps in Toss Console remote call, no Codex, no AIT upload, no API/Worker/DB modification, no runtime switch, no service restart, no Git commit/push/reset/stash.
