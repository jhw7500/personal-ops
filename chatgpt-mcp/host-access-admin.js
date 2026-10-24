'use strict';
/* Owner web controls for installation-wide, expiring host-user access.
 * This UI never issues access requests or starts commands/models.
 */
(() => {
  const $ = id => document.getElementById(id);
  const root = $('host-access');
  if (!root) return;

  const ID = /^[0-9a-f]{32}$/;
  const TERMINAL = new Set(['succeeded', 'failed', 'cancelled', 'timed_out', 'outcome_unknown']);
  const MAX_LOG_TEXT = 65536;
  let project = '';
  let generation = 0;
  let access = null;
  let executions = [];
  let executionDetail = null;
  let selectedRequest = '';
  let selectedExecution = '';
  let readController = null;
  let actionController = null;
  let actionBusy = false;
  let displayedGrant = '';
  let displayedIdentity = '';
  let logKey = '';
  let logCursor = 0;
  let logText = '';
  let requestedExecution = '';
  // This URL carries only a public request ID; it never grants authority.
  const incomingFocus = window.PersonalOpsHostAccessFocus;
  const focusedRequest = incomingFocus && typeof incomingFocus.project === 'string'
    && ID.test(incomingFocus.request_id || '') ? incomingFocus : null;
  let focusHandled = false;
  let focusScrolled = false;
  let focusedRequestMissing = false;

  function errorMessage(data, response) {
    return data && typeof data.code === 'string' && /^[A-Z][A-Z0-9_]{2,63}$/.test(data.code)
      ? data.code : '요청이 거부됐습니다 (' + response.status + ')';
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {'Content-Type': 'application/json'},
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(errorMessage(data, response));
    return data;
  }

  function current(generationAtStart, projectAtStart) {
    return generationAtStart === generation && projectAtStart === project
      && projectAtStart === $('project').value;
  }

  function selectedRequestData() {
    const rows = Array.isArray(access?.requests) ? access.requests : [];
    return rows.find(row => row && row.request_id === selectedRequest) || null;
  }

  function selectedExecutionData() {
    return executions.find(row => row && row.operation_id === selectedExecution) || null;
  }

  function validHostIdentity(identity) {
    const label = (value, limit) => typeof value === 'string' && value.length > 0
      && value.length <= limit && !/[\u0000-\u001f\u007f]/.test(value);
    return Boolean(identity && typeof identity === 'object'
      && label(identity.hostname, 255)
      && label(identity.user, 128)
      && Number.isSafeInteger(identity.uid) && identity.uid >= 0
      && Array.isArray(identity.groups) && identity.groups.every(
        group => label(group, 128))
      && identity.filesystem_isolated === false
      && identity.effective_access === 'host_user_filesystem_network_groups'
      && identity.passwordless_sudo_verified === false);
  }

  function validMinutes() {
    const value = Number($('host-access-duration').value);
    return Number.isInteger(value) && value >= 1 && value <= 43200 ? value : null;
  }

  function hasFreshApproval() {
    return Boolean(project && project === $('project').value
      && $('host-access-confirmation').value === project
      && $('host-access-token').value.trim() && !actionBusy);
  }

  function clearFreshApproval() {
    $('host-access-confirmation').value = '';
    $('host-access-token').value = '';
  }

  function localAccessStatus() {
    const hasGrant = access?.active === true && ID.test(access.grant_id || '');
    const lifetime = access?.lifetime || 'timed';
    const expiry = lifetime === 'timed' ? Date.parse(access?.expires_at || '') : Infinity;
    const validExpiry = lifetime === 'until_revoked'
      ? access?.version === 2 && access?.expires_at === null
      : lifetime === 'timed' && Number.isFinite(expiry);
    return {hasGrant, lifetime, expiry, validExpiry,
      active: hasGrant && validExpiry && (lifetime === 'until_revoked' || expiry > Date.now()),
      expired: hasGrant && lifetime === 'timed' && validExpiry && expiry <= Date.now()};
  }

  function renderButtons() {
    const fresh = hasFreshApproval();
    const pending = selectedRequestData();
    const lifetime = pending?.lifetime || 'timed';
    const validDuration = lifetime === 'until_revoked' || validMinutes();
    $('host-access-approve').disabled = !(fresh && validHostIdentity(access?.host_identity)
      && pending && validDuration);
    $('host-access-revoke').disabled = !(fresh && localAccessStatus().active);
    const selected = executionDetail?.operation_id === selectedExecution
      ? executionDetail : selectedExecutionData();
    $('host-execution-cancel').disabled = !(fresh && selected && ID.test(selectedExecution)
      && !TERMINAL.has(selected.state));
  }

  function renderRequestOptions() {
    const pending = selectedRequestData();
    const requestedLifetime = pending?.lifetime || 'timed';
    $('host-access-lifetime').value = requestedLifetime;
    $('host-access-duration').disabled = requestedLifetime === 'until_revoked';
  }

  function renderAccess() {
    const select = $('host-access-request');
    const previousRequest = selectedRequest;
    const rows = Array.isArray(access?.requests) ? access.requests.filter(
      row => row && ID.test(row.request_id || '') && typeof row.expires_at === 'string') : [];
    if (!focusHandled && focusedRequest && project === focusedRequest.project
        && access?.project === project) {
      focusHandled = true;
      if (rows.some(row => row.request_id === focusedRequest.request_id)) {
        selectedRequest = focusedRequest.request_id;
      } else {
        selectedRequest = '';
        focusedRequestMissing = true;
      }
    }
    if (!rows.some(row => row.request_id === selectedRequest)) {
      selectedRequest = focusedRequestMissing ? '' : rows.length ? rows[0].request_id : '';
    }
    select.replaceChildren();
    if (focusedRequestMissing) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = '링크의 요청이 만료됐거나 존재하지 않습니다';
      select.append(option);
    }
    for (const row of rows) {
      const option = document.createElement('option');
      option.value = row.request_id;
      option.textContent = row.request_id + ' · 만료 ' + row.expires_at;
      select.append(option);
    }
    select.value = selectedRequest;
    renderRequestOptions();
    if (focusHandled && focusedRequest && project === focusedRequest.project
        && access?.project === project && !focusScrolled) {
      focusScrolled = true;
      root.scrollIntoView?.({block: 'start'});
    }
    const nextGrant = access?.active === true && ID.test(access.grant_id || '')
      ? access.grant_id : '';
    const identity = access?.host_identity;
    const nextIdentity = validHostIdentity(identity) ? JSON.stringify(identity) : '';
    if (previousRequest !== selectedRequest || displayedGrant !== nextGrant
        || displayedIdentity !== nextIdentity) clearFreshApproval();
    displayedGrant = nextGrant;
    displayedIdentity = nextIdentity;

    if (validHostIdentity(identity)) {
      const groups = identity.groups.length ? identity.groups.join(', ') : '없음';
      $('host-access-identity').textContent = '호스트 ' + identity.hostname
        + ' · 사용자 ' + identity.user + ' · uid ' + identity.uid + ' · 그룹 ' + groups + '\n'
        + '이 설치 전체의 호출자는 이 호스트 사용자 계정의 파일시스템, 네트워크, 그룹 권한을 그대로 사용합니다. '
        + '표시된 docker/sudo 그룹이 있으면 해당 그룹 권한도 포함됩니다. '
        + 'passwordless sudo는 검증되지 않았으며 sudo 그룹 멤버십만으로 증명되지 않습니다.';
    } else {
      $('host-access-identity').textContent = project
        ? '호스트 사용자 권한 정보를 확인할 수 없어 액세스를 승인할 수 없습니다.'
        : '프로젝트를 선택하면 호스트 사용자 권한 정보를 표시합니다.';
    }

    if (!project) {
      $('host-access-state').textContent = '프로젝트를 선택하세요.';
      $('host-access-guidance').textContent = '';
    } else if (localAccessStatus().active) {
      const lifetimeText = access.lifetime === 'until_revoked'
        ? '소유자가 취소할 때까지' : '만료 ' + String(access.expires_at || '');
      $('host-access-state').textContent = '활성 · ' + lifetimeText
        + ' · Codex ' + (access.allow_codex === true ? '허용' : '허용 안 함');
      $('host-access-guidance').textContent = rows.length
        ? '새 요청을 승인하면 현재 액세스 시간이 새 승인으로 교체됩니다.'
        : '승인만으로 명령이나 Codex가 시작되지는 않습니다.';
    } else {
      const reasons = {
        NO_GRANT: '활성 액세스 없음', REVOKED: '액세스 취소됨', EXPIRED: '액세스 만료됨',
        RUNTIME_CHANGED: '설치 런타임 변경으로 비활성', BOOT_CHANGED: '재부팅으로 비활성',
        PROJECT_CHANGED: '프로젝트 문맥 변경으로 비활성', UID_CHANGED: '호스트 사용자 변경으로 비활성',
        UNSAFE_RUNTIME: '검증된 설치 런타임 아님', BOOT_ID_UNAVAILABLE: '부팅 식별자 확인 불가',
      };
      const local = localAccessStatus();
      const reason = local.expired ? '액세스 만료됨'
        : local.hasGrant && !local.validExpiry ? '액세스 만료 시각 확인 불가'
        : reasons[access?.reason] || String(access?.reason || '상태 확인 불가');
      $('host-access-state').textContent = reason + ' · 승인 대기 요청 ' + rows.length + '개';
      $('host-access-guidance').textContent = rows.length
        ? '요청을 선택하고 프로젝트 이름과 새 관리자 토큰을 입력해 승인하세요.'
        : 'ChatGPT에서 request_host_access를 호출해 요청을 만든 뒤 한 번 승인하세요.';
    }
    if (focusedRequestMissing && focusedRequest && project === focusedRequest.project) {
      $('host-access-guidance').textContent = '링크의 요청이 만료됐거나 사라졌습니다. 다른 요청을 실수로 승인하지 않도록 자동 선택하지 않았습니다. ChatGPT에서 새 요청을 생성하세요.';
    }
    renderOverviewAccess();
    renderButtons();
  }

  function renderOverviewAccess() {
    const state = $('overview-access-state');
    const codex = $('overview-codex-state');
    if (!state || !codex) return;
    if (!project) {
      state.textContent = '프로젝트 선택 필요';
      codex.textContent = '호스트 Codex 권한 확인 전';
      return;
    }
    const local = localAccessStatus();
    const left = local.expiry - Date.now();
    if (local.active) {
      const remaining = local.lifetime === 'until_revoked' ? '소유자가 취소할 때까지'
        : left >= 86400000
        ? `${Math.floor(left / 86400000)}일 ${Math.floor((left % 86400000) / 3600000)}시간 남음`
        : left >= 3600000
          ? `${Math.floor(left / 3600000)}시간 ${Math.floor((left % 3600000) / 60000)}분 남음`
          : `${Math.floor(left / 60000)}분 ${Math.floor((left % 60000) / 1000)}초 남음`;
      state.textContent = `활성 · ${remaining}`;
      codex.textContent = access.allow_codex === true
        ? '호스트 Codex 허용 · 격리 준비 상태와 별개'
        : '호스트 Codex 허용 안 함 · 격리 준비 상태와 별개';
      return;
    }
    if (local.hasGrant) {
      state.textContent = local.validExpiry ? '액세스 만료됨' : '액세스 만료 시각 확인 불가';
      codex.textContent = '호스트 Codex 허용 안 함 · 격리 준비 상태와 별개';
      return;
    }
    const labels = {NO_GRANT: '활성 액세스 없음', REVOKED: '액세스 취소됨', EXPIRED: '액세스 만료됨'};
    state.textContent = labels[access?.reason] || (access ? '호스트 액세스 비활성' : '상태 확인 불가');
    codex.textContent = '호스트 Codex 허용 안 함 · 격리 준비 상태와 별개';
  }

  function executionWorkContext(row) {
    const value = row?.work_context;
    const text = item => typeof item === 'string' && item.trim().length > 0
      && Array.from(item).length <= 160 && !/\p{C}/u.test(item);
    const actions = ['read', 'search', 'reason', 'code', 'build', 'test', 'review', 'execute', 'commit', 'push'];
    if (!value || typeof value !== 'object' || Array.isArray(value)
        || !text(value.command_label) || !actions.includes(value.action)
        || !(value.label_source === 'reported' && text(value.task_label)
          || value.label_source === 'unavailable' && value.task_label === null)) return null;
    return {task_label: value.task_label, label_source: value.label_source,
      command_label: value.command_label, action: value.action};
  }

  function safeExecution(row) {
    if (!row) return null;
    const context = executionWorkContext(row);
    return {
      project: row.project,
      operation_id: row.operation_id,
      kind: row.kind,
      state: row.state,
      phase: row.phase,
      code: row.code ?? null,
      created_at: row.created_at ?? null,
      started_at: row.started_at ?? null,
      finished_at: row.finished_at ?? null,
      ...(context ? {work_context: context} : {}),
    };
  }

  function resetLog() {
    logKey = '';
    logCursor = 0;
    logText = '';
    $('host-execution-log').textContent = '';
  }

  function renderExecutions() {
    const select = $('host-execution');
    const rows = executions.filter(row => row && ID.test(row.operation_id || ''));
    select.replaceChildren();
    for (const row of rows) {
      const option = document.createElement('option');
      option.value = row.operation_id;
      const context = executionWorkContext(row);
      const title = context?.task_label || context?.command_label || '작업 목적 미수집';
      option.textContent = title + ' · ' + String(row.state || 'unknown')
        + ' · ' + row.operation_id.slice(0, 8);
      select.append(option);
    }
    const selectedPending = selectedExecution && !rows.some(row => row.operation_id === selectedExecution)
      && (requestedExecution === selectedExecution || executionDetail?.operation_id === selectedExecution);
    if (selectedPending) {
      const option = document.createElement('option');
      option.value = selectedExecution;
      option.textContent = selectedExecution + ' · '
        + (executionDetail?.state ? String(executionDetail.state) : '새 실행 상태 확인 중');
      select.append(option);
    } else if (!rows.some(row => row.operation_id === selectedExecution)) {
      selectedExecution = rows.length ? rows[0].operation_id : '';
      executionDetail = null;
      resetLog();
    }
    select.value = selectedExecution;
    const selected = executionDetail?.operation_id === selectedExecution
      ? executionDetail : selectedExecutionData();
    $('host-execution-state').textContent = selected
      ? JSON.stringify(safeExecution(selected), null, 2) : '선택한 실행이 없습니다.';
    renderButtons();
  }

  function switchProject(nextProject) {
    readController?.abort();
    actionController?.abort();
    generation += 1;
    project = nextProject || '';
    access = null;
    executions = [];
    executionDetail = null;
    selectedRequest = '';
    selectedExecution = '';
    requestedExecution = '';
    displayedGrant = '';
    displayedIdentity = '';
    actionBusy = false;
    clearFreshApproval();
    $('host-access-action-status').textContent = '';
    resetLog();
    renderAccess();
    renderExecutions();
  }

  async function loadSelected(generationAtStart, projectAtStart, signal) {
    if (!selectedExecution || !ID.test(selectedExecution)) return;
    const operationId = selectedExecution;
    const streamValue = $('host-execution-stream').value;
    const stream = streamValue === 'stderr' ? 'stderr' : 'stdout';
    $('host-execution-stream').value = stream;
    const key = projectAtStart + ':' + operationId + ':' + stream;
    if (logKey !== key) {
      resetLog();
      logKey = key;
    }
    const cursor = logCursor;
    const base = 'project=' + encodeURIComponent(projectAtStart)
      + '&operation_id=' + encodeURIComponent(operationId);
    const [detail, log] = await Promise.all([
      request('/api/host-execution?' + base, {signal}),
      request('/api/host-execution-log?' + base + '&stream=' + stream
        + '&cursor=' + encodeURIComponent(String(cursor)), {signal}),
    ]);
    if (!current(generationAtStart, projectAtStart) || operationId !== selectedExecution
        || key !== logKey || signal.aborted) return;
    if (detail?.project !== projectAtStart || detail?.operation_id !== operationId) {
      throw new Error('HOST_EXECUTION_SCOPE_MISMATCH');
    }
    executionDetail = detail;
    requestedExecution = '';
    const chunk = typeof log?.text === 'string' ? log.text
      : typeof log?.content === 'string' ? log.content : '';
    const next = Number(log?.next_cursor);
    if (cursor === logCursor) {
      logText = (logText + chunk).slice(-MAX_LOG_TEXT);
      if (Number.isSafeInteger(next) && next >= cursor) logCursor = next;
      $('host-execution-log').textContent = logText;
    }
    renderExecutions();
  }

  async function refresh() {
    const selectedProject = $('project').value;
    if (selectedProject !== project) switchProject(selectedProject);
    if (!selectedProject) return;
    readController?.abort();
    const controller = new AbortController();
    readController = controller;
    const generationAtStart = generation;
    const query = 'project=' + encodeURIComponent(selectedProject);
    try {
      const [accessResult, executionResult] = await Promise.allSettled([
        request('/api/host-access?' + query, {signal: controller.signal}),
        request('/api/host-executions?' + query, {signal: controller.signal}),
      ]);
      if (!current(generationAtStart, selectedProject) || controller.signal.aborted) return;
      const errors = [];
      if (accessResult.status === 'fulfilled') {
        if (accessResult.value?.project !== selectedProject) throw new Error('HOST_ACCESS_SCOPE_MISMATCH');
        access = accessResult.value;
      } else {
        access = null;
        errors.push(accessResult.reason);
      }
      if (executionResult.status === 'fulfilled') {
        const listed = executionResult.value;
        if (listed?.project !== selectedProject) throw new Error('HOST_EXECUTION_SCOPE_MISMATCH');
        executions = Array.isArray(listed.executions)
          ? listed.executions.filter(row => row?.project === selectedProject) : [];
      } else {
        errors.push(executionResult.reason);
      }
      renderAccess();
      renderExecutions();
      if (executionResult.status === 'fulfilled') {
        await loadSelected(generationAtStart, selectedProject, controller.signal);
      }
      if (errors.length) {
        clearFreshApproval();
        renderButtons();
        $('host-access-action-status').textContent = errors.map(error => error.message).join(' · ');
      } else {
        $('host-access-action-status').textContent = '';
      }
    } catch (error) {
      if (!current(generationAtStart, selectedProject) || controller.signal.aborted) return;
      access = null;
      executions = [];
      executionDetail = null;
      selectedRequest = '';
      selectedExecution = '';
      displayedGrant = '';
      displayedIdentity = '';
      resetLog();
      clearFreshApproval();
      renderAccess();
      renderExecutions();
      $('host-access-action-status').textContent = String(error.message || '호스트 액세스 조회 실패');
    }
  }

  async function mutate(path, body, successMessage, expected) {
    if (actionBusy || !hasFreshApproval()) return;
    const projectAtStart = project;
    const generationAtStart = generation;
    if (!expected()) return;
    actionController?.abort();
    const controller = new AbortController();
    actionController = controller;
    actionBusy = true;
    renderButtons();
    try {
      await request(path + '?project=' + encodeURIComponent(projectAtStart), {
        method: 'POST', signal: controller.signal, body: JSON.stringify(body),
      });
      if (current(generationAtStart, projectAtStart) && !controller.signal.aborted && expected()) {
        $('host-access-action-status').textContent = successMessage;
      }
    } catch (error) {
      if (current(generationAtStart, projectAtStart) && !controller.signal.aborted) {
        $('host-access-action-status').textContent = String(error.message || '요청 실패');
      }
    } finally {
      if (current(generationAtStart, projectAtStart)) {
        actionBusy = false;
        $('host-access-token').value = '';
        $('host-access-confirmation').value = '';
        renderButtons();
        await refresh();
      }
    }
  }

  $('host-access-request').addEventListener('change', () => {
    focusedRequestMissing = false;
    selectedRequest = $('host-access-request').value;
    clearFreshApproval();
    renderRequestOptions();
    renderButtons();
  });
  $('host-execution').addEventListener('change', () => {
    selectedExecution = $('host-execution').value;
    executionDetail = null;
    clearFreshApproval();
    resetLog();
    renderExecutions();
    void refresh();
  });
  $('host-execution-stream').addEventListener('change', () => {
    resetLog();
    void refresh();
  });
  for (const id of ['host-access-confirmation', 'host-access-token', 'host-access-duration']) {
    $(id).addEventListener('input', renderButtons);
  }
  $('host-access-codex').addEventListener('change', renderButtons);
  $('host-access-refresh').addEventListener('click', () => { void refresh(); });
  $('project').addEventListener('change', () => { void refresh(); });
  window.addEventListener('personal-ops:select-host-execution', event => {
    const detail = event?.detail;
    if (!detail || detail.project !== project || detail.project !== $('project').value
        || !ID.test(detail.operationId || '')) return;
    selectedExecution = detail.operationId;
    requestedExecution = detail.operationId;
    executionDetail = null;
    clearFreshApproval();
    resetLog();
    renderExecutions();
    void refresh();
  });

  $('host-access-approve').addEventListener('click', () => {
    const pending = selectedRequestData();
    const lifetime = pending?.lifetime || 'timed';
    const minutes = lifetime === 'timed' ? validMinutes() : null;
    const expectedProject = project;
    if (!pending || lifetime === 'timed' && !minutes
        || !validHostIdentity(access?.host_identity)) return;
    const requestId = pending.request_id;
    void mutate('/api/host-access-approve', {
      request_id: requestId,
      lifetime,
      duration_seconds: minutes === null ? null : minutes * 60,
      allow_codex: $('host-access-codex').checked === true,
      confirmation: expectedProject,
      admin_token: $('host-access-token').value,
    }, '호스트 액세스를 승인했습니다. 실행은 ChatGPT의 별도 요청으로만 시작됩니다.',
    () => project === expectedProject && selectedRequest === requestId);
  });

  $('host-access-revoke').addEventListener('click', () => {
    const grantId = access?.grant_id;
    const expectedProject = project;
    if (!localAccessStatus().active || !ID.test(grantId || '')) return;
    void mutate('/api/host-access-revoke', {
      grant_id: grantId,
      confirmation: expectedProject,
      admin_token: $('host-access-token').value,
    }, '호스트 액세스를 취소했습니다. 이미 발생한 외부 변경은 되돌아가지 않습니다.',
    () => project === expectedProject && access?.grant_id === grantId && localAccessStatus().active);
  });

  $('host-execution-cancel').addEventListener('click', () => {
    const operationId = selectedExecution;
    const expectedProject = project;
    if (!ID.test(operationId || '')) return;
    void mutate('/api/host-execution-cancel', {
      operation_id: operationId,
      confirmation: expectedProject,
      admin_token: $('host-access-token').value,
    }, '선택한 실행의 중지를 요청했습니다.',
    () => project === expectedProject && selectedExecution === operationId);
  });

  new MutationObserver(() => {
    if (!$('dashboard').hidden && $('project').value !== project) void refresh();
  }).observe($('project'), {childList: true});
  setInterval(() => {
    if ($('dashboard').hidden || document.hidden) return;
    if ($('dashboard').dataset.following === 'false') {
      renderAccess();
      return;
    }
    return refresh();
  }, 3000);
  $('host-execution-stream').value = 'stdout';
  renderAccess();
  renderExecutions();
})();
