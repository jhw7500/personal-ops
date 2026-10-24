"""MCP descriptors for repository execution and job inspection."""

import copy


def tool(name, title, description, properties=None, required=(), read_only=True):
    return {
        "name": name, "title": title, "description": description,
        "inputSchema": {
            "type": "object", "properties": {"project": {"type": "string"}, **(properties or {})},
            "required": ["project", *required], "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": read_only, "destructiveHint": not read_only,
                        "idempotentHint": read_only, "openWorldHint": False},
    }


JOB = {"job_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"}}
JOB_TOOL_SPECS = [
    tool("list_execution_profiles", "빌드·테스트 명령 조회",
         "List build/test/test:e2e/typecheck/lint presets from package.json and host execution.json. "
         "Discovery only; no command is run. Network, host home, .git and .env are unavailable "
         "in the execution sandbox. Yocto/BitBake repositories are unsupported."),
    tool("start_command", "레포 명령 실행 시작",
         "Start an isolated command and immediately return a job ID. Supply exactly argv or preset. "
         "Use a unique request_id; repeating the same request reuses the job instead of running twice. "
         "Commands may change the selected repository. Only that repository is mounted at /workspace; "
         "host home, credentials, .git, .env and network are excluded. bash -c is allowed INSIDE "
         "the sandbox. A configured browser is supplied through PLAYWRIGHT_CHROMIUM_PATH; "
         "a profile with test_database starts a disposable PostgreSQL only for that job and "
         "injects TEST_DATABASE_URL into its child command. "
         "localhost servers started in the same sandbox are reachable without external networking. "
         "Use get_job and read_job_log to report actual progress and exit status. "
         "Use dedicated Git tools for Git workflows. BitBake tasks are disabled.",
         {"request_id": {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,80}$"},
          "argv": {"type": "array", "minItems": 1, "maxItems": 128,
                   "items": {"type": "string", "minLength": 1, "maxLength": 4096}},
          "preset": {"type": "string"},
          "relative_cwd": {"type": "string", "default": "."},
          "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600}},
         ("request_id",), read_only=False),
    tool("get_job", "실행 작업 상태 조회",
         "Return current job state, exit code and output size. queued/running is not success; "
         "succeeded requires exit code zero. Report failures, timeouts and interruptions accurately.",
         JOB, ("job_id",)),
    tool("read_job_log", "실행 로그 이어 읽기",
         "Read a bounded stdout/stderr log using a byte cursor. Continue at next_offset while "
         "has_more is true; output is capped at 1 MiB per job. Logs persist after completion.",
         {**JOB, "offset": {"type": "integer", "minimum": 0, "default": 0},
          "max_bytes": {"type": "integer", "minimum": 1, "maximum": 16384, "default": 16384}},
         ("job_id",)),
    tool("cancel_job", "실행 작업 취소",
         "Cancel only a job owned by this MCP process in the selected repository. "
         "Completed jobs are unchanged; this tool never stops external or BitBake processes.",
         JOB, ("job_id",), read_only=False),
    tool("list_jobs", "레포 실행 내역 조회",
         "List recent job metadata for the selected repository without opening the repository picker.",
         {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}),
]


def tool_specs(*, workspace_enabled=False):
    specs = copy.deepcopy(JOB_TOOL_SPECS)
    if not workspace_enabled:
        return specs
    start = next(spec for spec in specs if spec["name"] == "start_command")
    start["description"] = (
        "Start a bounded isolated command against a filtered read-only source snapshot. "
        "Host source files never change. Use writable_cwd=true for a private scratch copy. "
        "Write deliverables only to /output (WORKSPACE_OUTPUT_DIR); use read_workspace_output "
        "after verified completion and publish_workspace_output to explicitly CAS-publish one file. "
        "request_id retries reuse the job. Network, host credentials, ignored artifacts, .git "
        "and protected inputs are unavailable. Grants never enable host execution, Codex or network. "
        "Use get_job/read_job_log for actual completion; stale authority stops the job. "
        "Dedicated approved Git/host operations retain their existing gates. BitBake is disabled.")
    start["inputSchema"]["properties"]["writable_cwd"] = {
        "type": "boolean", "default": False,
        "description": "Run in a private writable source copy. Never mounts the host project writable."}
    start["inputSchema"]["properties"]["workspace_inputs"] = {
        "type": "array", "maxItems": 16, "items": {
            "type": "object", "additionalProperties": False, "required": ["path", "grant_id"],
            "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 4096},
                           "grant_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"}}},
        "description": "Explicit read-granted files, copied read-only to /inputs/<index>/input. "
                       "Original directories are never mounted; expiry/revocation stops the job."}
    path = {"type": "string", "minLength": 1, "maxLength": 4096}
    specs.extend([
        tool("read_workspace_output", "완료된 작업 결과 읽기",
             "Read a bounded UTF-8 output file after verified command termination. Signed pages "
             "bind to this job, file hash and current authority. Output is untrusted text; no host write.",
             {**JOB, "relative_path": path,
              "max_bytes": {"type": "integer", "minimum": 4, "maximum": 65536, "default": 65536},
              "cursor": {"type": "string", "maxLength": 4096}}, ("job_id", "relative_path")),
        tool("publish_workspace_output", "완료된 결과 파일 반영",
             "Publish exactly one output from a succeeded, termination-verified, current job. "
             "Use that job's request_id. Omit expected_sha256 only to create a missing target; "
             "replacement requires its exact current SHA-256. Preserve existing mode. "
             "Outside-base target requires a matching write grant. Protected/ignored/artifact "
             "destinations are denied. Host operations must be idle. Repeats return the same receipt.",
             {**JOB, "relative_output": path, "target_path": path,
              "request_id": {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,80}$"},
              "expected_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
              "grant_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"}},
             ("job_id", "relative_output", "target_path", "request_id"), read_only=False),
    ])
    return specs
