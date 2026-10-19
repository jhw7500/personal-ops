# tossApp full deploy profile correction - 2026-10-15

STATUS: the real `tossApp-deploy test` command is registered as
`tossapp-deploy-test` and subsequently completed its entire workflow through
actual MCP execution after the user said `진행해서 배포까지해`. Version
20261015-23 is CREATED/isTested=true, command exit 0 and termination verified.
See [the full live result](2026-10-15-tossapp-full-test-deployment.md).
The registration-only checks below remain historical evidence; no upload was
performed until that subsequent execution authorization.

## Correction to the previous implementation

The user requested registration of the existing full deployment command.
The earlier implementation registered a fixed-artifact uploader instead.
That successfully uploaded version 20261015-22, but packaging and SELF push
were performed separately. It did not connect the requested full command.
The user explicitly corrected that scope. Host registration and necessary
input-mode adjustments were completed without another permission handoff.

| Profile | Executable | Arguments | Behavior |
| --- | --- | --- | --- |
| tossapp-deploy-test | /home/jhw/.local/bin/tossApp-deploy | test | Fresh build + TEST upload + SELF invitation |
| tossapp-upload-test | Installed pinned upload wrapper | Fixed artifact pins | Existing bundle upload only |

The full command uses the current working tree, not the archived option-2
frontend. It executes typecheck/Vite/AIT packaging, requires a newly generated
nonempty artifact stamp, uploads `ai-tarot/tarororo.ait`, saves the returned
deployment link and invokes the existing `tossApp-invite` for that same ID.
The script has its own command lock and stops after a failed step. If upload
succeeds but invitation fails, its existing `invite` action retries invitation
without rebuilding/uploading. That separate action is not registered here.

Host console OAuth supplies the SELF invitation. The web conversation does
not need a separate Toss console plugin for this full command. Login, console
review, production release and arbitrary argv are not exposed by this profile.
The existing scripts were not modified.

## Applied policy

- Policy: `~/.config/chatgpt-coding-mcp/host-commands.json`, regular owner-only
  0600, still enabled/version 1.
- Script SHA-256:
  `2b7043727b73c0c1ff63e91761e346b400276acb431decde686a8c27762794dc`.
- Arguments: exactly `["test"]`; cwd is tossApp root; effect external_publish;
  timeout 1800 seconds.
- Previous policy SHA: 44b9b33787b7b8fd91686cd65a801efae4e0448fa70582a07f37a0a3e895e730.
- Current policy SHA: 1d232f5770c5cc25a6b0e86866ecdbcbf550987e5ec7c6e0d83a503340ef03f3.
- Existing uploader settings/artifact pins were preserved under
  `tossapp-upload-test`; only its public label was clarified.
- The shared config writer lock, expected-digest comparison, private temporary
  file, candidate freeze/verify, atomic replace and directory fsync were used.
  No grant or approval policy was widened.

Ten build configuration inputs are pinned: root package.json/package-lock.json,
ai-tarot/package.json, apps-in-toss.config.ts, vite.config.ts, tsconfig.json,
tsconfig.app.json, tsconfig.node.json, scripts/production-api-env.ts and .nvmrc.
All were owner-owned regular files with mode 0664. The existing input verifier
rejected that group-write permission. Their contents/owners were preserved
and permissions changed to 0644; the verifier was not relaxed. Changes to these
recipe/config inputs require an owner-reviewed profile hash update. Application
source is the script's current build input; this profile does not claim to freeze
every frontend file or a not-yet-built artifact SHA before approval.

The full command retains the existing script's upload behavior. It does not
claim the fixed uploader's separately verified private dependency closure or
sealed AIT-upload proof. No independent remote digest is claimed for it.

## Registration verification (historical; before the full live run)

- Actual list_host_command_profiles reports the two profiles and clear scopes.
- Actual prepare_host_command for the full profile created
  `c1bf7c3c2b64fb3627730bce4e297d10`. Its private frozen executable/argv/input
  map exactly matches the installed full profile, with approval required.
- Installed-runtime freeze/verify_frozen passed for both profiles.
- Readonly native entry probe ran the real script's status action with exit 0
  and existing host credentials available. The immutable entry source is
  non-executable in the release; it was correctly invoked with Python -I for
  this probe (normal runtime copies it to an executable private entry).
- Existing production API environment points at the same TEST host; Node is
  24.12.0; deployment credentials and matching SELF-console OAuth exist.
- No actual TEST command or new approval receipt was created in this pass.

The running-process cancellation tool leaves pending approvals unchanged.
To avoid blocking the user's next plan, the host operator revoked only the
exact unapproved verification plan under the manager's operations lock after
checking its ID/request/digests, current frozen profile, absent approval receipt,
absent execution directory/identity and absent started_at. Before/after evidence
is retained; its original plan digest/nonce/timestamps are preserved. Actual
get_host_command now reports cancelled/COMPLETE, with no execution result and
no project lease. No running process or unrelated plan was touched. This is
recorded host cleanup, not a claim that the MCP pending-cancel tool worked.

## Next web invocation

Ask `tossApp에서 tossapp-deploy-test 프로필로 새 빌드부터 TEST 업로드와 본인
초대장 발송까지 실행할 계획을 만들어줘`. Use a fresh request ID. The existing
one-use management-web approval is still required, then execute_host_command
starts the whole script and get_host_command reports its numeric final result.
Do not approve or execute the cancelled verification operation.

At registration time protected scripts, original AIT, credentials, runtime
config, all 16 agent-proof source files and source content were preserved, with
no Docker/DB action, service restart, paid agent call or Git stage/commit/push.
Version 20261015-22 was then the last verified upload. The subsequently approved
full run generated a new AIT and deployed version 20261015-23 as linked above.
Phone receipt and QA remain unobserved.

Private evidence directory:
`/home/jhw/.local/state/personal-ops-rollout-recovery/20261015/tossapp-real-deploy-profile-je8_umfv`
(0700, JSON reports are regular owner-owned 0600). It contains before/after
policy and input fingerprints, registration.json, validated-profiles.json,
connector-list-prepare.json, connector-verification.json,
sealed-script-status-probe.json and verification-plan-cleanup.json.
