'use strict';
// Registered host-operation rendering. Every server-provided string goes through
// textContent; nothing here parses HTML. Exposed for the node:vm UI tests.
globalThis.HostOperationsView = (() => {
  const OPERATION_ID = /^[0-9a-f]{32}$/;
  const TARGET = /^[a-z0-9][a-z0-9_-]{0,62}$/;
  const PROJECT = /^[A-Za-z0-9_.-]{1,64}$/;
  const CODE = /^[A-Z][A-Z0-9_]{2,63}$/;
  const DIAGNOSTIC = /^[0-9a-f]{32}$/;
  const STATES = {pending_host_approval: '승인 대기', approved: '승인됨 · 채팅 실행 대기', queued: '실행 대기열',
    running: '실행 중', succeeded: '성공', failed: '실패', rejected: '거절됨', expired: '만료됨',
    outcome_unknown: '결과 불명'};
  const PHASES = {APPROVAL: '승인', QUEUED: '대기열', PRECHECK: '사전 검사', BACKUP: '백업', EXECUTE: '실행',
    VERIFY: '검증', COMPLETE: '완료', RECOVER: '복구 확인'};
  const CHANGES = {added: '추가', modified: '수정', deleted: '삭제'};
  const OMITTED = {binary: '바이너리 파일', too_large: '크기 한도 초과', review_budget: '검토 자료 한도 초과',
    unavailable: '비교할 내용 없음'};
  const stateLabel = state => STATES[state] || String(state);
  const phaseLabel = phase => (phase ? `${PHASES[phase] || phase} (${phase})` : '—');
  const join = values => (Array.isArray(values) && values.length ? values.map(String).join(', ') : '없음');
  function parseFocus(pathname, search) {
    if (pathname !== '/host-operations') return null;
    const params = new URLSearchParams(search);
    const keys = [...params.keys()];
    if (keys.length !== 3 || new Set(keys).size !== 3) return null;
    const focus = {project: params.get('project'), target: params.get('target'), operation_id: params.get('operation_id')};
    if (!focus.project || !focus.target || !focus.operation_id || !PROJECT.test(focus.project)
        || !TARGET.test(focus.target) || !OPERATION_ID.test(focus.operation_id)) return null;
    return focus;
  }
  function parseLegacyFocus(pathname, search) {
    if (pathname !== '/host-operations') return null;
    const params = new URLSearchParams(search);
    const keys = [...params.keys()];
    if (keys.length !== 3 || new Set(keys).size !== 3
        || !keys.includes('project') || !keys.includes('target')
        || !keys.includes('host_operation_id')) return null;
    const focus = {project: params.get('project'), target: params.get('target'),
      operation_id: params.get('host_operation_id')};
    if (focus.project !== 'tossApp' || focus.target !== 'tarororo-test'
        || !OPERATION_ID.test(focus.operation_id || '')) return null;
    return focus;
  }
  function parseHostAccessFocus(pathname, search) {
    if (pathname !== '/host-access') return null;
    const params = new URLSearchParams(search);
    const keys = [...params.keys()];
    if (keys.length !== 2 || new Set(keys).size !== 2
        || !keys.includes('project') || !keys.includes('request_id')) return null;
    const focus = {project: params.get('project'), request_id: params.get('request_id')};
    return PROJECT.test(focus.project || '') && OPERATION_ID.test(focus.request_id || '')
      ? focus : null;
  }
  function remaining(expiresAt, nowMs) {
    const left = Date.parse(expiresAt) - nowMs;
    if (!Number.isFinite(left) || left <= 0) return {expired: true, text: '만료됨'};
    const seconds = Math.floor(left / 1000);
    return {expired: false, text: `${Math.floor(seconds / 60)}분 ${seconds % 60}초 남음`};
  }
  function publicError(error) {
    const code = error && typeof error.code === 'string' && CODE.test(error.code) ? error.code : '';
    const diagnostic = error && typeof error.diagnostic_id === 'string' && DIAGNOSTIC.test(error.diagnostic_id)
      ? error.diagnostic_id : '';
    if (code) return `오류 코드 ${code}${diagnostic ? ` · 진단 ID ${diagnostic}` : ''}`;
    if (error && error.status === 401) return '관리 화면에 다시 로그인하세요.';
    return error && Number.isInteger(error.status) ? `요청을 처리하지 못했습니다 (${error.status}).` : '요청을 처리하지 못했습니다.';
  }
  function canDecide(operation, nowMs) {
    return Boolean(operation) && operation.state === 'pending_host_approval' && !remaining(operation.expires_at, nowMs).expired;
  }
  function element(doc, tag, text, className) {
    const node = doc.createElement(tag);
    if (text !== undefined) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }
  function section(doc, title, rows) {
    const box = element(doc, 'div', undefined, 'op-section');
    box.append(element(doc, 'h4', title));
    const list = doc.createElement('dl');
    for (const [label, value] of rows) {
      list.append(element(doc, 'dt', label));
      const cell = doc.createElement('dd');
      if (value && typeof value === 'object' && value.tagName) cell.append(value); else cell.textContent = String(value);
      list.append(cell);
    }
    box.append(list);
    return box;
  }
  function bullets(doc, title, values) {
    const box = element(doc, 'div', undefined, 'op-section');
    box.append(element(doc, 'h4', title));
    const list = doc.createElement('ul');
    for (const value of Array.isArray(values) && values.length ? values : ['없음']) list.append(element(doc, 'li', value));
    box.append(list);
    return box;
  }
  function renderReviewFiles(doc, container, review, title = '고위험 변경 검토 자료') {
    container.replaceChildren();
    if (!review || !Array.isArray(review.files)) {
      container.textContent = '검토 자료를 불러오지 않았습니다.';
      return;
    }
    const box = element(doc, 'div', undefined, 'op-section');
    box.append(element(doc, 'h4', `${title} (${review.files.length})`));
    if (!review.files.length) box.append(element(doc, 'p', '고위험 변경 없음 · 빈 검토 자료가 확인되었습니다.', 'muted'));
    for (const file of review.files) {
      const hashes = [file.head_sha256 ? `HEAD SHA ${file.head_sha256}` : null,
        file.snapshot_sha256 ? `스냅샷 SHA ${file.snapshot_sha256}` : null,
        file.head_mode ? `HEAD 모드 ${file.head_mode}` : null,
        typeof file.snapshot_executable === 'boolean'
          ? `스냅샷 실행 비트 ${file.snapshot_executable}` : null].filter(Boolean).join(' · ');
      box.append(element(doc, 'p', `${file.path} · ${CHANGES[file.change] || file.change} · HEAD ${file.head_size ?? '—'} bytes → 스냅샷 ${file.snapshot_size ?? '—'} bytes${hashes ? ` · ${hashes}` : ''}`));
      if (file.diff_omitted) box.append(element(doc, 'p', `diff 생략: ${OMITTED[file.diff_omitted] || file.diff_omitted}`, 'muted'));
      else box.append(element(doc, 'pre', String(file.diff || '') + (file.diff_truncated ? '\n… (검토 자료 한도로 잘림)' : ''), 'op-diff'));
    }
    container.append(box);
  }
  function countdown(doc, expiresAt, nowMs, countdowns) {
    const node = element(doc, 'span', remaining(expiresAt, nowMs).text, 'countdown');
    countdowns.push({node, expiresAt});
    return node;
  }
  function operationError(operation) {
    if (!operation.code && !operation.diagnostic_id) return '';
    return [`오류 코드 ${CODE.test(operation.code || '') ? operation.code : '—'}`, `단계 ${phaseLabel(operation.phase)}`,
      ...(DIAGNOSTIC.test(operation.diagnostic_id || '') ? [`진단 ID ${operation.diagnostic_id}`] : [])].join(' · ');
  }
  function renderList(doc, container, operations, {selected, nowMs, countdowns, onSelect}) {
    container.replaceChildren();
    if (!operations.length) {
      container.textContent = '이 대상의 등록 작업이 없습니다. ChatGPT에서 prepare_host_operation으로 계획을 만드세요.';
      return;
    }
    for (const operation of operations) {
      const button = element(doc, 'button', `${operation.action} · ${stateLabel(operation.state)}`, 'op');
      button.type = 'button';
      button.dataset.id = operation.operation_id;
      button.dataset.state = operation.state;
      button.classList.toggle('selected', operation.operation_id === selected);
      const detail = element(doc, 'small', `${operation.operation_id} · 단계 ${phaseLabel(operation.phase)} · `);
      if (operation.state === 'pending_host_approval') detail.append(countdown(doc, operation.expires_at, nowMs, countdowns));
      else detail.append(operation.finished_at || operation.started_at || operation.approved_at || operation.created_at || '');
      button.append(detail);
      button.addEventListener('click', () => onSelect(operation.operation_id));
      container.append(button);
    }
  }
  function renderDetail(doc, container, operation, review, {nowMs, countdowns}) {
    container.replaceChildren();
    if (!operation) {
      container.textContent = '작업을 선택하세요.';
      return;
    }
    const expired = operation.state === 'expired' || (operation.state === 'pending_host_approval'
      && remaining(operation.expires_at, nowMs).expired);
    const notes = {pending_host_approval: '승인 대기 중입니다. 아래 내용을 확인한 뒤 작업 ID와 관리자 토큰을 다시 입력해 승인하거나 거절하세요.',
      approved: '승인 영수증이 발급됐습니다. 실행은 ChatGPT에서 execute_host_operation 호출로만 시작됩니다.',
      queued: '실행 대기열에 있습니다.', running: '실행 중입니다. 이벤트로 단계를 확인하세요.',
      succeeded: '사후 검증까지 성공했습니다.', failed: '실패했습니다. 오류 코드와 진단 ID만 표시합니다.',
      rejected: '거절되었거나 계획이 더 이상 유효하지 않습니다.',
      outcome_unknown: '실제 효과를 확인하지 못했습니다. 자동 재실행하지 않으며 상태를 읽기 전용으로 확인해야 합니다.'};
    container.append(element(doc, 'p', `${operation.project}/${operation.target} · ${operation.action} · ${stateLabel(operation.state)} · ${operation.operation_id}`, 'op-title'));
    container.append(element(doc, 'p', expired ? '만료됨 · 이 계획은 승인할 수 없습니다. ChatGPT에서 새 계획을 만들어야 합니다.'
      : notes[operation.state] || '', 'muted'));
    const status = [['상태', stateLabel(operation.state)], ['단계', phaseLabel(operation.phase)]];
    const failure = operationError(operation);
    if (failure) status.push(['오류', failure]);
    container.append(section(doc, '상태', status));
    const parameters = operation.parameters || {};
    container.append(section(doc, '계획', [
      ['동작', operation.action],
      ['대상 서비스', parameters.services ? join(parameters.services) : '—'],
      ['실행 프로필', parameters.profile || '—'],
      ['요청 ID', operation.request_id],
      ['생성', operation.created_at],
      ['만료', expired ? `${operation.expires_at} · 만료됨` : (() => {
        const box = doc.createElement('span');
        box.append(`${operation.expires_at} · `, countdown(doc, operation.expires_at, nowMs, countdowns));
        return box;
      })()],
      ['계획 digest', operation.plan_digest],
      ['소스 digest', operation.source_digest],
      ['설정 digest', operation.config_digest],
    ]));
    const impact = operation.impact || {};
    container.append(section(doc, '영향', [
      ['재생성', join(impact.recreated)], ['중단', join(impact.interrupted)], ['중지', join(impact.stopped)],
      ['보존', join(impact.preserved)], ['실행 서비스', impact.exec_service || '—'],
      ['DB 연결', impact.db_connections === 'interrupted' ? '끊김 (interrupted)' : '영향 없음 (unaffected)'],
      ['볼륨', impact.volumes === 'preserved' ? '보존 (preserved)' : String(impact.volumes)],
    ]));
    container.append(bullets(doc, '검증 항목', operation.verifications));
    const source = operation.source || {};
    container.append(section(doc, '소스 검토', [
      ['정책', source.review_policy], ['Git HEAD', source.git_head || '—'], ['고정 파일 수', source.file_count],
      ['변경 경로', `${source.changed_path_count ?? 0}개`], ['고위험 변경', join(source.high_risk_changes)],
    ]));
    container.append(bullets(doc, '변경 경로', source.changed_paths));
    if (review && Array.isArray(review.files)) {
      const reviewBox = element(doc, 'div');
      renderReviewFiles(doc, reviewBox, review);
      container.append(reviewBox);
    }
    if (operation.result && typeof operation.result === 'object') {
      container.append(section(doc, '결과', Object.entries(operation.result).map(([key, value]) =>
        [key, Array.isArray(value) ? join(value) : String(value)])));
    }
  }
  function renderEvents(doc, container, events, append) {
    if (!append) container.replaceChildren();
    for (const event of events) {
      const fields = Object.entries(event.fields || {}).map(([key, value]) =>
        `${key}=${Array.isArray(value) ? value.join('/') : value}`).join(' ');
      container.append(element(doc, 'li', `#${event.cursor} · ${event.at} · ${phaseLabel(event.phase)} · ${event.code}${fields ? ' · ' + fields : ''}`));
    }
    if (!container.children.length) container.textContent = '기록된 이벤트가 없습니다.';
  }
  return {parseFocus, parseLegacyFocus, parseHostAccessFocus, remaining, publicError, canDecide, stateLabel, renderList, renderDetail,
    renderEvents, renderReviewFiles};
})();
(() => {
  const view = globalThis.HostOperationsView;
  const focusLink = view.parseFocus(location.pathname, location.search);
  const legacyFocusLink = view.parseLegacyFocus(location.pathname, location.search);
  const runtimeRolloutFocus = (() => {
    if (location.pathname !== '/runtime-rollouts') return null;
    const params = new URLSearchParams(location.search);
    const keys = [...params.keys()];
    const operationId = params.get('operation_id');
    return keys.length === 1 && keys[0] === 'operation_id'
      && /^[0-9a-f]{32}$/.test(operationId || '') ? operationId : null;
  })();
  const hostAccessFocusLink = view.parseHostAccessFocus(location.pathname, location.search);
  globalThis.PersonalOpsHostAccessFocus = hostAccessFocusLink;
  const fragment = new URLSearchParams(location.hash.slice(1));
  let initialToken = fragment.get('token') || '';
  if (location.hash) history.replaceState(null, '', location.pathname + location.search);
  const $ = id => document.getElementById(id);
  const s = {token: '', connected: false, project: '', job: '', offset: 0, revision: 0, dirty: false,
    epoch: 0, jobEpoch: 0, following: true, fastTimer: null, detailTimer: null,
    fastBusy: false, detailBusy: false, saving: false, memoryReady: false, memoryLoading: false,
    pendingFast: false, pendingDetail: false, pendingProgress: false, pendingGit: false, projects: [],
    progressRevision: 0, progressDirty: false, progressReady: false, progressLoading: false,
    progressSaving: false, displayedRevision: -1, gitTime: 0, file: '', diffEpoch: 0, handoffBusy: false,
    mapJobs: [], mapEvents: [], mapRecords: [], mapHostExecutions: [], mapHostObservedAt: null,
    mapSummary: null, mapHostError: false, registeredTimer: null};
  // Reasons that only mean "nothing registered here": the panel stays hidden without targets.
  const QUIET_REASONS = new Set(['HOST_CONFIG_MISSING', 'REGISTRY_NOT_CONFIGURED', 'UNSAFE_RUNTIME']);
  const reg = {epoch: 0, targets: [], target: '', operations: [], selected: '', detail: null, detailKey: '',
    cursor: 0, more: false, skew: 0, busy: false, pending: false, deciding: false, listCountdowns: [], detailCountdowns: [],
    countdowns: [], focus: focusLink};
  const progressInputs = ['progress-title', 'progress-state', 'progress-summary', 'progress-completed', 'progress-next'];
  const viewNames = ['workroom', 'overview', 'executions', 'access', 'repository'];
  let activeView = focusLink || legacyFocusLink || hostAccessFocusLink || runtimeRolloutFocus ? 'access' : 'workroom';
  let workMap = null;
  const mapShouldAnimate = () => s.following && !document.hidden
    && activeView === 'workroom';
  function activateView(name, focusTab = false) {
    if (!viewNames.includes(name)) return;
    activeView = name;
    for (const view of viewNames) {
      const selected = view === name;
      const tab = $(`nav-${view}`);
      const panel = $(`view-${view}`);
      tab.setAttribute('aria-selected', selected ? 'true' : 'false');
      tab.tabIndex = selected ? 0 : -1;
      panel.hidden = !selected;
    }
    $('work-map').hidden = name !== 'workroom';
    $('overview-current-work').hidden = name !== 'overview';
    workMap?.setLive(mapShouldAnimate());
    if (focusTab) $(`nav-${name}`).focus();
  }
  for (const view of viewNames) {
    $(`nav-${view}`).addEventListener('click', () => activateView(view));
    $(`nav-${view}`).addEventListener('keydown', event => {
      const current = viewNames.indexOf(view);
      const next = event.key === 'ArrowRight' ? (current + 1) % viewNames.length
        : event.key === 'ArrowLeft' ? (current + viewNames.length - 1) % viewNames.length
        : event.key === 'Home' ? 0 : event.key === 'End' ? viewNames.length - 1 : -1;
      if (next >= 0) { event.preventDefault(); activateView(viewNames[next], true); }
    });
  }
  activateView(activeView);
  workMap = window.WorkroomBridge.create($('workroom-frame'), {onSummary: summary => {
    s.mapSummary = summary;
    renderOperatingSummary();
  }, onJobSelect: id => {
    activateView('executions');
    selectJob(id);
    $('execution-details').scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start'});
  }, onHostExecutionSelect: operationId => {
    if (!/^[0-9a-f]{32}$/.test(operationId || '') || !s.project) return;
    activateView('executions');
    window.dispatchEvent(new CustomEvent('personal-ops:select-host-execution', {
      detail: {project: s.project, operationId},
    }));
  }});
  workMap.setLive(mapShouldAnimate());
  const phases = {started: '시작', completed: '완료', failed: '실패', canceled: '취소'};
  const states = {queued: '대기', running: '실행 중', canceling: '취소 중', canceled: '취소됨',
    succeeded: '성공', failed: '실패', timed_out: '시간 초과', interrupted: '중단됨'};
  const workStates = {not_recorded: '기록 없음', planned: '계획됨', in_progress: '진행 중', blocked: '진행 막힘', completed: '완료'};
  function notice(message = '') { $('notice').textContent = message; }
  function controls() {
    $('save').disabled = !s.connected || !s.memoryReady || !s.dirty || s.saving || s.memoryLoading;
    $('reload-memory').disabled = !s.connected || s.saving || s.memoryLoading;
    $('memory').disabled = !s.connected || !s.memoryReady || s.memoryLoading;
    for (const id of progressInputs) $(id).disabled = !s.connected || !s.progressReady || s.progressLoading || s.progressSaving;
    $('save-progress').disabled = !s.connected || !s.progressReady || !s.progressDirty || s.progressLoading || s.progressSaving;
    $('reload-progress').disabled = !s.connected || s.progressLoading || s.progressSaving;
    $('make-handoff').disabled = !s.connected || s.handoffBusy;
    $('copy-handoff').disabled = !s.connected || !$('handoff').value;
  }
  async function api(path, options = {}) {
    const headers = {'Content-Type': 'application/json'};
    if (s.token) headers.Authorization = `Bearer ${s.token}`;
    const response = await fetch(path, {...options, signal: AbortSignal.timeout(10000), cache: 'no-store', credentials: 'same-origin', headers});
    if (!response.ok) {
      const error = new Error(response.status === 401 ? '관리 화면에 다시 로그인하세요.' :
        response.status === 409 ? '다른 곳에서 저장본이 변경됐습니다. 입력을 보존했습니다. 저장본을 다시 읽고 내용을 합쳐주세요.' :
        `요청을 처리하지 못했습니다 (${response.status}).`);
      error.status = response.status;
      try {
        const detail = await response.json();
        if (detail && typeof detail.code === 'string') error.code = detail.code;
        if (detail && typeof detail.diagnostic_id === 'string') error.diagnostic_id = detail.diagnostic_id;
      } catch { /* Error bodies are optional. */ }
      throw error;
    }
    return response.json();
  }
  const query = project => `project=${encodeURIComponent(project)}`;
  function rememberProject(project) { try { localStorage.setItem('personal_ops_project', project); } catch { /* Optional preference only. */ } }
  function rememberedProject() { try { return localStorage.getItem('personal_ops_project'); } catch { return null; } }
  function memoryDisplay(data) {
    $('memory').value = data.content;
    s.revision = data.revision; s.dirty = false; s.memoryReady = true;
    $('memory-status').textContent = `저장본 v${data.revision}${data.updated_at ? ' · ' + data.updated_at : ' · 아직 저장된 메모가 없습니다'}`;
    controls();
  }
  async function loadMemory(epoch) {
    const data = await api(`/api/memory?${query(s.project)}`);
    if (epoch === s.epoch) memoryDisplay(data);
  }
  function listItems(id, values) {
    $(id).replaceChildren();
    for (const value of values.length ? values : ['기록된 항목이 없습니다.']) {
      const li = document.createElement('li'); li.textContent = value;
      if (!values.length) li.className = 'empty';
      $(id).append(li);
    }
  }
  function progressDisplay(data, replaceEditor = false) {
    if (data.revision < s.displayedRevision) return;
    s.displayedRevision = data.revision;
    $('work-title').textContent = data.title || '기록된 작업 목표가 없습니다';
    $('work-summary').textContent = data.summary || (data.revision ? '' : '실행 내역은 자동으로 표시되지만, 작업 목표와 완료 항목은 아직 저장되지 않았습니다. 채팅에 “현재 목표와 완료 항목, 다음 작업을 작업 보고에 기록하고 단계마다 갱신해줘”라고 요청하거나 직접 입력하세요.');
    $('work-state').textContent = workStates[data.status] || data.status;
    $('work-updated').textContent = data.updated_at ? `저장된 작업 보고 · v${data.revision} · ${data.updated_at}` : '이전 채팅 내용을 자동으로 작업 목표로 추정하지 않습니다.';
    listItems('completed', data.completed); listItems('next-steps', data.next_steps);
    if (replaceEditor || (!s.progressDirty && !s.progressSaving && !s.progressLoading)) {
      $('progress-title').value = data.title;
      $('progress-state').value = data.status === 'not_recorded' ? 'in_progress' : data.status;
      $('progress-summary').value = data.summary;
      $('progress-completed').value = data.completed.join('\n');
      $('progress-next').value = data.next_steps.join('\n');
      s.progressRevision = data.revision; s.progressDirty = false; s.progressReady = true;
      $('progress-status').textContent = `저장본 v${data.revision}`;
    }
    controls();
  }
  async function loadProgress(epoch) {
    const data = await api(`/api/progress?${query(s.project)}`);
    if (epoch === s.epoch) progressDisplay(data, true);
  }
  function selectJob(id, refreshNow = true) {
    if (refreshNow) $('execution-details').open = true;
    if (s.job === id) return;
    s.job = id; s.jobEpoch++; s.offset = 0;
    $('log').textContent = ''; $('log-status').textContent = '출력 불러오는 중…';
    document.querySelectorAll('.job').forEach(el => el.classList.toggle('selected', el.dataset.id === id));
    if (refreshNow) refreshDetails(false, false);
  }
  function jobButton(job) {
    const button = document.createElement('button'); button.type = 'button';
    button.className = 'job'; button.dataset.id = job.job_id; button.dataset.state = job.state;
    button.classList.toggle('selected', job.job_id === s.job);
    button.textContent = job.preset || job.label || job.job_id;
    const detail = document.createElement('small');
    detail.textContent = `${states[job.state] || job.state} · 종료 코드 ${job.exit_code ?? '—'} · ${job.relative_cwd || '.'} · ${job.finished_at || job.started_at || job.created_at || ''}`;
    button.append(detail); button.addEventListener('click', () => selectJob(job.job_id));
    return button;
  }
  function renderJobs(data) {
    const selectedDefault = !s.job && data.jobs.length;
    if (selectedDefault) selectJob(data.jobs[0].job_id, false);
    $('jobs').replaceChildren(); $('job-count').textContent = `(${data.jobs.length})`;
    if (!data.jobs.length) $('jobs').textContent = '저장된 실행 작업이 없습니다.';
    for (const job of data.jobs) $('jobs').append(jobButton(job));
    const count = states => data.jobs.filter(job => states.includes(job.state)).length;
    $('observed-status').textContent = data.jobs.length ?
      `최근 ${data.jobs.length}개 · 성공 ${count(['succeeded'])} · 실패 ${count(['failed', 'timed_out'])} · 진행 중 ${count(['queued', 'running', 'canceling'])} · 중단/취소 ${count(['canceled', 'interrupted'])}` :
      '저장된 터널 실행 기록이 없습니다. 호스트 터미널에서 직접 실행한 명령은 이 목록에 자동 기록되지 않습니다.';
    $('recent-work').replaceChildren();
    for (const job of data.jobs.slice(0, 5)) $('recent-work').append(jobButton(job));
    $('checks').replaceChildren();
    const checks = data.jobs.filter(job => /(^|[\s:/._-])(test|tests|typecheck|type-check|lint|build|e2e|check|검사|테스트|빌드)([\s:/._-]|$)/i.test(`${job.preset || ''} ${job.label || ''}`)).slice(0, 20);
    if (!checks.length) $('checks').textContent = '저장된 빌드·테스트·검사 실행이 없습니다. 다른 명령은 전체 실행 작업에서 확인하세요.';
    for (const job of checks) $('checks').append(jobButton(job));
    if (selectedDefault) refreshDetails(false, false);
  }
  function renderActivity(data) {
    $('activity').replaceChildren();
    if (!data.events.length && !(data.records || []).length) $('activity').textContent = '기록된 MCP 호출이 없습니다.';
    for (const event of data.events) {
      const li = document.createElement('li');
      li.textContent = `${event.time} · ${event.tool} · ${phases[event.phase] || event.phase}`;
      $('activity').append(li);
    }
    for (const record of data.records || []) {
      const li = document.createElement('li');
      li.textContent = `${record.time} · ${record.actor_label} · ${record.task_label} · ${record.state} · ${record.provenance === 'observed' ? '직접 관측' : '보고됨'}`;
      $('activity').append(li);
    }
  }
  function clearDiff() {
    s.file = ''; s.diffEpoch++; $('diff').textContent = ''; $('diff-status').textContent = '파일을 선택하세요.';
  }
  async function loadDiff() {
    if (!s.file || !s.connected) return;
    const epoch = s.epoch, diffEpoch = ++s.diffEpoch, file = s.file, staged = $('diff-mode').value;
    $('diff').textContent = ''; $('diff-status').textContent = `${file} · 불러오는 중…`;
    try {
      const result = await api(`/api/diff?${query(s.project)}&path=${encodeURIComponent(file)}&staged=${staged}`);
      if (epoch !== s.epoch || diffEpoch !== s.diffEpoch) return;
      $('diff').textContent = result.text;
      $('diff-status').textContent = `${file} · ${result.observed_at}${result.text ? '' : ' · 선택한 범위에 변경 내용이 없습니다'}${result.truncated ? ' · 미리보기 한도로 잘렸습니다' : ''}`;
    } catch (error) { if (epoch === s.epoch && diffEpoch === s.diffEpoch) $('diff-status').textContent = error.message; }
  }
  function renderChanges(data) {
    $('changes').replaceChildren(); $('change-count').textContent = data.available ? `(${data.files.length})` : '';
    $('git-status').textContent = data.available ? `${data.branch} · ${data.head ? data.head.slice(0, 10) : '커밋 없음'} · ${data.observed_at}${data.truncated ? ' · 파일 목록 일부 생략' : ''}` : data.error;
    if (!data.available || !data.files.length) {
      $('changes').textContent = data.available ? '변경 파일이 없습니다.' : 'Git 변경을 표시할 수 없습니다.';
      clearDiff(); return;
    }
    if (s.file && !data.files.some(row => row.path === s.file)) clearDiff();
    for (const row of data.files) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'file';
      button.classList.toggle('selected', row.path === s.file); button.dataset.path = row.path;
      button.textContent = row.path;
      const detail = document.createElement('small');
      detail.textContent = `${row.status === '??' ? '새 파일' : `Git 상태 ${row.status}`}${row.previous_path ? ' · 이전 경로 ' + row.previous_path : ''}`;
      button.append(detail); button.addEventListener('click', () => {
        s.file = row.path;
        document.querySelectorAll('.file').forEach(el => el.classList.toggle('selected', el.dataset.path === row.path));
        loadDiff();
      });
      $('changes').append(button);
    }
    if (s.file) loadDiff();
  }
  function pollingEnabled() {
    return s.following && s.connected && s.project && !document.hidden;
  }
  function clearPollingTimers() {
    clearTimeout(s.fastTimer); clearTimeout(s.detailTimer); clearTimeout(s.registeredTimer);
  }
  function scheduleFast() {
    clearTimeout(s.fastTimer);
    if (pollingEnabled()) s.fastTimer = setTimeout(refreshFast, 1000);
  }
  function scheduleDetails() {
    clearTimeout(s.detailTimer);
    if (pollingEnabled()) s.detailTimer = setTimeout(refreshDetails, 5000);
  }
  function renderOperatingSummary(hostError = s.mapHostError) {
    if (s.mapSummary && s.project) {
      $('overview-active-count').textContent = String(s.mapSummary.active);
      $('overview-failure-count').textContent = String(s.mapSummary.failed);
    } else {
      $('overview-active-count').textContent = '—';
      $('overview-failure-count').textContent = '—';
    }
    if (s.mapHostObservedAt) {
      $('overview-observation-state').textContent = hostError
        ? `마지막 성공 ${s.mapHostObservedAt} · 현재 조회 지연`
        : `관측 ${s.mapHostObservedAt}`;
    } else {
      $('overview-observation-state').textContent = hostError
        ? '호스트 실행 상태를 확인하지 못했습니다.' : '관측된 호스트 실행이 없습니다.';
    }
  }
  async function refreshFast() {
    if (!s.connected || !s.project) return;
    if (s.fastBusy) { s.pendingFast = true; return; }
    clearTimeout(s.fastTimer); s.fastBusy = true;
    const epoch = s.epoch, project = s.project;
    const parts = [
      {path: '/api/jobs', render: renderJobs, error: 'checks', extraError: 'observed-status'},
      {path: '/api/activity', render: renderActivity, error: 'activity'},
      {path: '/api/host-executions', render: () => {}, error: 'overview-observation-state', host: true}
    ];
    try {
      const results = await Promise.allSettled(parts.map(part => api(`${part.path}?${query(project)}`).then(data => {
        if (part.host && (data?.project !== project || !Array.isArray(data?.executions))) {
          throw new Error('호스트 실행 응답의 프로젝트 범위가 일치하지 않습니다.');
        }
        return data;
      })));
      if (epoch !== s.epoch) return;
      results.forEach((result, index) => {
        if (result.status === 'fulfilled') parts[index].render(result.value);
        else {
          $(parts[index].error).textContent = result.reason.message;
          if (parts[index].extraError) $(parts[index].extraError).textContent = result.reason.message;
        }
      });
      const jobs = results[0], activity = results[1];
      if (jobs.status === 'fulfilled') s.mapJobs = jobs.value.jobs;
      if (activity.status === 'fulfilled') {
        s.mapEvents = activity.value.events; s.mapRecords = activity.value.records || [];
      }
      const host = results[2];
      if (host.status === 'fulfilled') {
        s.mapHostExecutions = host.value.executions.filter(row => row?.project === project);
        s.mapHostObservedAt = new Date().toISOString();
      }
      const partial = results.some(result => result.status === 'rejected');
      s.mapHostError = host.status === 'rejected';
      renderOperatingSummary(s.mapHostError);
      workMap.update({project, projectEpoch: epoch, jobs: s.mapJobs, events: s.mapEvents, records: s.mapRecords,
        hostExecutions: s.mapHostExecutions, hostObservedAt: s.mapHostObservedAt, following: mapShouldAnimate(),
        error: partial ? '일부 실행·호출 기록을 확인하지 못했습니다.' : ''});
      if (epoch === s.epoch) $('updated').textContent = `갱신 ${new Date().toLocaleTimeString()}`;
    } finally {
      s.fastBusy = false; updateCountdowns();
      if (s.pendingFast) { s.pendingFast = false; queueMicrotask(refreshFast); }
      else scheduleFast();
    }
  }
  async function refreshDetails(forceGit = false, includeProgress = true) {
    if (!s.connected || !s.project) return;
    if (s.detailBusy) {
      s.pendingDetail = true; s.pendingGit ||= forceGit; s.pendingProgress ||= includeProgress; return;
    }
    clearTimeout(s.detailTimer); s.detailBusy = true;
    const epoch = s.epoch, project = s.project, jobEpoch = s.jobEpoch, id = s.job;
    const readGit = forceGit || Date.now() - s.gitTime >= 10000;
    const parts = [];
    if (includeProgress && !s.progressLoading) {
      parts.push({path: '/api/progress', render: progressDisplay, error: 'work-updated'});
    }
    if (readGit) {
      s.gitTime = Date.now(); parts.push({path: '/api/changes', render: renderChanges, error: 'git-status'});
    }
    if (id) {
      parts.push({path: `/api/log?${query(project)}&job_id=${encodeURIComponent(id)}&offset=${s.offset}`,
        render: result => {
          if (jobEpoch !== s.jobEpoch) return;
          s.offset = result.next_offset;
          $('log').textContent = ($('log').textContent + result.text).slice(-1024 * 1024);
          if (s.following) $('log').scrollTop = $('log').scrollHeight;
          $('log-status').textContent = `${states[result.state] || result.state} · ${s.offset} bytes${result.has_more ? ' · 남은 출력 불러오는 중' : ''}${result.log_truncated ? ' · 저장 한도로 출력이 잘렸습니다' : ''}`;
        }, error: 'log-status', valid: () => jobEpoch === s.jobEpoch});
    }
    try {
      const results = await Promise.allSettled(parts.map(part => api(part.path.includes('?') ? part.path : `${part.path}?${query(project)}`)));
      if (epoch !== s.epoch) return;
      results.forEach((result, index) => {
        const part = parts[index];
        if (part.valid && !part.valid()) return;
        if (result.status === 'fulfilled') part.render(result.value);
        else $(part.error).textContent = result.reason.message;
      });
    } finally {
      s.detailBusy = false;
      if (s.pendingDetail) {
        const git = s.pendingGit, progress = s.pendingProgress;
        s.pendingDetail = false; s.pendingGit = false; s.pendingProgress = false;
        queueMicrotask(() => refreshDetails(git, progress));
      } else scheduleDetails();
    }
  }
  function refresh(forceGit = false) { refreshFast(); refreshDetails(forceGit); }
  async function switchProject(project) {
    s.project = project; s.epoch++; s.jobEpoch++; s.job = ''; s.offset = 0; s.gitTime = 0;
    s.dirty = false; s.memoryReady = false; s.memoryLoading = true; s.revision = 0;
    s.progressDirty = false; s.progressReady = false; s.progressLoading = true; s.progressRevision = 0; s.displayedRevision = -1;
    s.handoffBusy = false; rememberProject(project); resetRegistered();
    s.mapJobs = []; s.mapEvents = []; s.mapRecords = []; s.mapHostExecutions = []; s.mapHostObservedAt = null;
    s.mapSummary = null; s.mapHostError = false;
    workMap.clear(project, s.epoch);
    renderOperatingSummary();
    const epoch = s.epoch;
    $('project').value = project; $('memory').value = ''; controls();
    for (const id of progressInputs) $(id).value = id === 'progress-state' ? 'in_progress' : '';
    $('memory-status').textContent = '저장본 불러오는 중…'; $('progress-status').textContent = '저장본 불러오는 중…';
    $('work-title').textContent = '작업 목표 불러오는 중…'; $('work-summary').textContent = ''; $('work-state').textContent = '';
    $('work-updated').textContent = ''; listItems('completed', []); listItems('next-steps', []);
    $('log').textContent = ''; $('jobs').replaceChildren(); $('checks').textContent = '실행 결과 불러오는 중…'; $('activity').replaceChildren();
    $('recent-work').replaceChildren(); $('observed-status').textContent = '실행 기록 불러오는 중…';
    $('changes').textContent = '변경 파일 불러오는 중…'; $('git-status').textContent = ''; $('change-count').textContent = '';
    clearDiff(); $('diff-mode').value = 'false'; $('handoff').value = ''; $('handoff-status').textContent = ''; controls();
    $('log-status').textContent = '작업을 선택하세요.';
    $('project-path').textContent = s.projects.find(p => p.project === project)?.path || '';
    notice();
    const loads = [
      (async () => { try { await loadMemory(epoch); } catch (error) { if (epoch === s.epoch) $('memory-status').textContent = error.message; }
        finally { if (epoch === s.epoch) { s.memoryLoading = false; controls(); } } })(),
      (async () => { try { await loadProgress(epoch); } catch (error) { if (epoch === s.epoch) $('work-updated').textContent = error.message; }
        finally { if (epoch === s.epoch) { s.progressLoading = false; controls(); } } })()
    ];
    refreshFast(); refreshDetails(true, false); await Promise.all(loads);
  }
  let hostPlans = [], hostPolicy = null, hostReviewEpoch = 0, hostFocus = legacyFocusLink;
  let hostAuthorization = null, hostAuthorizationEpoch = 0, hostAuthorizationBusy = false;
  let hostReviewPlan = '', hostReviewReady = false;
  const selectedHostPlan = () => hostPlans.find(plan => plan.operation_id === $('host-plan').value);
  const needsHostReview = plan => plan?.source_review?.policy === 'reviewed_snapshot';
  const targetAuthorizedPlan = plan => plan?.authorization?.mode === 'target';
  const activeTargetAuthorizedPlan = plan => targetAuthorizedPlan(plan)
    && hostAuthorization?.supported === true && hostAuthorization.enabled === true
    && typeof hostAuthorization.grant_id === 'string'
    && hostAuthorization.grant_id === plan.authorization.grant_id;
  function updateHostApproval(plan = selectedHostPlan()) {
    $('host-approve-form').hidden = targetAuthorizedPlan(plan);
    $('host-approve-submit').disabled = !plan || (needsHostReview(plan)
      && (!hostReviewReady || hostReviewPlan !== plan.operation_id));
  }
  function renderTargetAuthorization() {
    const supported = hostAuthorization?.supported === true;
    const migrationChanged = hostAuthorization?.reason === 'MIGRATION_SOURCE_CHANGED';
    $('host-target-auth-controls').hidden = !supported;
    $('host-target-auth-enable').disabled = !supported || hostAuthorizationBusy
      || (hostAuthorization.enabled && !migrationChanged);
    $('host-target-auth-disable').disabled = !supported || hostAuthorizationBusy || !hostAuthorization.enabled;
    if (!supported) return;
    const scope = 'API · Worker · DB 006~013';
    $('host-target-auth-state').textContent = migrationChanged
      ? `승인한 DB 001~013 SQL 변경 감지 · 다시 허용 필요 · ${hostAuthorization.enabled ? 'API·Worker 권한은 유지됨' : '대상 권한 비활성'}`
      : hostAuthorization.enabled
      ? `채팅 자동 실행 허용 · ${scope} · ${hostAuthorization.granted_at || '허용 시각 없음'}`
      : `허용 안 됨 · ${scope} · 작업별 1회 승인이 필요합니다.`;
  }
  async function refreshTargetAuthorization() {
    const epoch = ++hostAuthorizationEpoch, projectEpoch = s.epoch;
    if (!s.connected || s.project !== 'tossApp') {
      hostAuthorization = null; renderTargetAuthorization(); return;
    }
    hostAuthorization = null;
    $('host-target-auth-controls').hidden = true;
    $('host-target-auth-enable').disabled = true;
    $('host-target-auth-disable').disabled = true;
    try {
      const result = await api('/api/host-target-authorization?project=tossApp&target=tarororo-test');
      if (epoch !== hostAuthorizationEpoch || projectEpoch !== s.epoch || s.project !== 'tossApp') return;
      if (!result || typeof result.supported !== 'boolean' || typeof result.enabled !== 'boolean'
          || result.project !== 'tossApp' || result.target !== 'tarororo-test') {
        throw new Error('권한 상태 형식이 올바르지 않습니다.');
      }
      hostAuthorization = result;
      $('host-target-auth-status').textContent = result.supported ? ''
        : `대상 권한을 사용할 수 없습니다${result.reason ? ` · ${result.reason}` : ''}`;
    } catch (error) {
      if (epoch !== hostAuthorizationEpoch || projectEpoch !== s.epoch) return;
      hostAuthorization = null;
      $('host-target-auth-status').textContent = `대상 권한 조회 실패: ${view.publicError(error)}`;
    }
    renderTargetAuthorization();
  }
  function renderHostPolicy() {
    const reviewed = hostPolicy?.mode === 'reviewed_snapshot';
    $('host-scope-controls').hidden = reviewed;
    $('host-predecessor-controls').hidden = reviewed;
    $('host-planning-policy').textContent = reviewed
      ? '등록형 reviewed_snapshot · API, Worker, DB 006~012, DB 013 계획 생성 가능 · 대상 허용 시 006~013 결합 계획 가능 · 계획 → 대상 허용 또는 관리 웹 승인 → 채팅 실행 · Git 커밋 불필요'
      : '기존 Git HEAD 정책 · 활성화된 기능 범위 안에서만 계획을 만들 수 있습니다.';
  }
  function renderHostPlans() {
    const enabled = s.connected && s.project === 'tossApp';
    $('host-approval').hidden = !enabled;
    if (!enabled) return;
    renderHostPolicy();
    const previous = $('host-plan').value;
    $('host-plan').replaceChildren();
    for (const plan of hostPlans) {
      const option = document.createElement('option');
      option.value = plan.operation_id;
      option.textContent = `${plan.component} · ${plan.operation_id} · ${plan.mode}`;
      $('host-plan').append(option);
    }
    const focused = hostFocus && hostPlans.some(plan => plan.operation_id === hostFocus.operation_id)
      ? hostFocus.operation_id : '';
    const missingFocus = Boolean(hostFocus && !focused);
    $('host-plan').value = missingFocus ? '' : focused || (hostPlans.some(plan => plan.operation_id === previous)
      ? previous : hostPlans[0]?.operation_id || '');
    void showHostDetails();
    if (hostFocus) {
      if (!focused) $('host-status').textContent = '링크의 계획이 만료됐거나 더 이상 승인 대기 상태가 아닙니다.';
      if (focused) hostFocus = null;
      $('host-approval').scrollIntoView({behavior: 'auto', block: 'start'});
    }
  }
  async function showHostDetails() {
    const epoch = ++hostReviewEpoch;
    const plan = selectedHostPlan();
    hostReviewPlan = plan?.operation_id || '';
    hostReviewReady = Boolean(plan) && !needsHostReview(plan);
    $('host-review').replaceChildren();
    $('host-details').textContent = plan ? JSON.stringify({
      target: 'tossApp/tarororo-test', component: plan.component,
      mode: plan.mode, plan_digest: plan.plan_digest,
      source_digest: plan.source_digest, expires_at: plan.expires_at,
      source_review_policy: plan.source_review?.policy || 'git_head (기존 계획)',
      authorization: plan.authorization || {mode: 'per_operation'},
      review_digest: plan.source_review?.review_digest || null,
      config_digest: plan.source_review?.config_digest || null,
      inventory_digest: plan.source_review?.inventory_digest || null,
      recreate: plan.impact.recreate, preserves: plan.impact.preserve,
      schema_change: plan.impact.schema_change || null,
      schema_scope: plan.component === 'schema_update' ? '006~013 순차 적용 · 하나의 결합 트랜잭션'
        : plan.component === 'predecessor_migration' ? '006~012만 실행 · 013 제외'
        : plan.component === 'migration' ? '013만 실행 · 006~012 제외'
        : plan.component === 'schema014' ? '014만 실행 · 백업·복원 검증 후 적용 · 서비스 재시작 없음'
        : 'DB 스키마 변경 없음',
      execution_effect: activeTargetAuthorizedPlan(plan)
        ? '대상 사전 허용 적용 · 실행은 채팅의 명시적 MCP 호출'
        : targetAuthorizedPlan(plan)
        ? '대상 권한이 변경되어 이 계획은 사용할 수 없음 · 새 계획 필요'
        : '승인 영수증만 발급 · 실행은 별도 MCP 호출',
      migration_checksums: plan.migration_checksums || null,
      database_generation: plan.database_generation || null,
      high_risk_changes: plan.high_risk_changes,
      changed_path_count: plan.changed_path_count,
    }, null, 2) : '승인 대기 중인 배포 계획이 없습니다.';
    if (!plan) {
      updateHostApproval(plan);
      return;
    }
    if (activeTargetAuthorizedPlan(plan)) {
      $('host-status').textContent = '대상 권한이 적용된 계획입니다. 1회 승인 없이 채팅에서 실행할 수 있습니다.';
    } else if (targetAuthorizedPlan(plan)) {
      $('host-status').textContent = '대상 권한이 변경되어 이 계획은 사용할 수 없습니다. 새 계획을 만드세요.';
    }
    if (!needsHostReview(plan)) {
      $('host-review').textContent = '기존 Git HEAD 계획 · 별도 스냅샷 검토 자료가 없는 이전 형식입니다.';
      updateHostApproval(plan);
      return;
    }
    $('host-review').textContent = '고정 소스 검토 자료를 불러오는 중…';
    updateHostApproval(plan);
    try {
      const data = await api(`/api/host-operation-review?project=tossApp&operation_id=${encodeURIComponent(plan.operation_id)}`);
      if (epoch !== hostReviewEpoch || $('host-plan').value !== plan.operation_id) return;
      if (data.operation_id !== plan.operation_id || !data.review
          || data.review.source_digest !== plan.source_review.inventory_digest
          || !Array.isArray(data.review.files)) {
        throw new Error('검토 자료 바인딩이 일치하지 않습니다.');
      }
      hostReviewReady = true;
      view.renderReviewFiles(document, $('host-review'), data.review, '고정 소스 변경 검토 자료');
    } catch (error) {
      if (epoch !== hostReviewEpoch || $('host-plan').value !== plan.operation_id) return;
      hostReviewReady = false;
      $('host-review').textContent = `검토 자료를 불러오지 못했습니다: ${view.publicError(error)}`;
      if (!targetAuthorizedPlan(plan)) {
        $('host-status').textContent = `검토 자료를 불러오지 못했습니다: ${view.publicError(error)}`;
      }
    }
    if (targetAuthorizedPlan(plan) && !activeTargetAuthorizedPlan(plan)) {
      $('host-status').textContent = '대상 권한이 변경되어 이 계획은 사용할 수 없습니다. 새 계획을 만드세요.';
    }
    updateHostApproval(plan);
  }
  async function refreshHostPlans() {
    if (!s.connected || s.project !== 'tossApp') {
      hostPlans = []; hostAuthorization = null; renderTargetAuthorization(); renderHostPlans(); return;
    }
    await refreshTargetAuthorization();
    try {
      const result = await api('/api/host-operations?project=tossApp');
      hostPlans = result.plans;
      hostPolicy = result.planning_policy || null;
      $('host-status').textContent = `승인 대기 ${hostPlans.length}건 · 웹 승인은 로컬 루프백 또는 지정된 tailnet HTTPS에서만 가능`;
    } catch (error) {
      hostPlans = []; hostPolicy = null; hostReviewEpoch++;
      $('host-status').textContent = `계획 조회 실패: ${error.message}`;
    }
    renderHostPlans();
  }
  const registeredNow = () => Date.now() + reg.skew;
  const targetQuery = () => `${query(s.project)}&target=${encodeURIComponent(reg.target)}`;
  function resetRegistered() {
    reg.epoch++; reg.targets = []; reg.target = ''; reg.operations = []; reg.selected = ''; reg.detail = null;
    reg.detailKey = ''; reg.cursor = 0; reg.more = false; reg.listCountdowns = []; reg.detailCountdowns = [];
    reg.countdowns = []; clearTimeout(s.registeredTimer);
    $('registered-ops').hidden = true; $('registered-target').replaceChildren(); $('registered-list').replaceChildren();
    $('registered-detail').replaceChildren(); $('registered-events').replaceChildren();
    $('registered-availability').textContent = ''; $('registered-status').textContent = '';
    $('registered-events-more').hidden = true; updateDecision();
  }
  function updateDecision() {
    const allowed = s.connected && !reg.deciding && view.canDecide(reg.detail, registeredNow());
    $('registered-approve').disabled = !allowed; $('registered-reject').disabled = !allowed;
  }
  function updateCountdowns() {
    for (const item of reg.countdowns) item.node.textContent = view.remaining(item.expiresAt, registeredNow()).text;
    updateDecision();
  }
  function scheduleRegistered() {
    clearTimeout(s.registeredTimer);
    if (pollingEnabled() && !$('registered-ops').hidden && reg.target) s.registeredTimer = setTimeout(refreshRegistered, 5000);
  }
  function renderRegisteredList() {
    const listCountdowns = [];
    view.renderList(document, $('registered-list'), reg.operations, {selected: reg.selected, nowMs: registeredNow(),
      countdowns: listCountdowns, onSelect: id => {
        reg.selected = id; reg.cursor = 0; reg.detailKey = ''; renderRegisteredList(); loadRegisteredDetail(true);
      }});
    reg.listCountdowns = listCountdowns;
    reg.countdowns = [...listCountdowns, ...reg.detailCountdowns];
  }
  async function loadRegisteredDetail(reset) {
    if (!reg.selected || !reg.target) return;
    const epoch = reg.epoch, id = reg.selected, cursor = reset ? 0 : reg.cursor;
    try {
      const data = await api(`/api/registered-operation?${targetQuery()}&operation_id=${encodeURIComponent(id)}&cursor=${cursor}`);
      if (epoch !== reg.epoch || id !== reg.selected) return;
      const skew = Date.parse(data.server_time) - Date.now();
      if (Number.isFinite(skew)) reg.skew = skew;
      reg.detail = data.operation;
      const key = JSON.stringify([data.operation, data.review]);
      if (reset || key !== reg.detailKey) {  // Keep the reader's place while nothing changed.
        const detailCountdowns = [];
        view.renderDetail(document, $('registered-detail'), data.operation, data.review, {nowMs: registeredNow(), countdowns: detailCountdowns});
        reg.detailCountdowns = detailCountdowns; reg.detailKey = key;
        reg.countdowns = [...reg.listCountdowns, ...detailCountdowns];
      }
      const fresh = data.events.events.filter(event => reset || event.cursor > reg.cursor);
      view.renderEvents(document, $('registered-events'), fresh, !reset);
      reg.cursor = Math.max(reset ? 0 : reg.cursor, data.events.next_cursor);
      reg.more = data.events.more;
      $('registered-events-more').hidden = !reg.more;
    } catch (error) {
      if (epoch !== reg.epoch || id !== reg.selected) return;
      reg.detail = null; reg.detailCountdowns = []; reg.detailKey = '';
      $('registered-detail').textContent = `작업을 불러오지 못했습니다: ${view.publicError(error)}`;
    }
    updateDecision();
  }
  async function refreshRegistered() {
    if (!s.connected || !s.project) return;
    if (reg.busy) { reg.pending = true; return; }
    reg.busy = true; clearTimeout(s.registeredTimer);
    const epoch = reg.epoch, project = s.project;
    const focus = reg.focus && reg.focus.project === project ? reg.focus : null;
    try {
      const data = await api(`/api/registered-targets?${query(project)}`);
      if (epoch !== reg.epoch) return;
      reg.targets = data.targets;
      $('registered-availability').textContent = data.enabled ? `등록 대상 ${data.targets.length}개`
        : `등록 대상 작업 제한 · 사유 코드 ${data.reason}`;
      $('registered-ops').hidden = !(data.targets.length || focus || (data.reason && !QUIET_REASONS.has(data.reason)));
      const ids = data.targets.map(target => target.target);
      reg.target = focus && ids.includes(focus.target) ? focus.target : ids.includes(reg.target) ? reg.target : ids[0] || '';
      $('registered-target').replaceChildren();
      for (const id of ids) {
        const option = document.createElement('option'); option.value = id; option.textContent = id;
        $('registered-target').append(option);
      }
      $('registered-target').value = reg.target;
      if (!reg.target) {
        reg.operations = []; reg.selected = ''; reg.detail = null;
        $('registered-list').textContent = focus ? '링크의 대상이 이 프로젝트에 등록되어 있지 않거나 비활성 상태입니다.' : '등록된 호스트 대상이 없습니다.';
        $('registered-detail').replaceChildren(); $('registered-events').replaceChildren();
        return;
      }
      const listing = await api(`/api/registered-operations?${targetQuery()}`);
      if (epoch !== reg.epoch) return;
      const skew = Date.parse(listing.server_time) - Date.now();
      if (Number.isFinite(skew)) reg.skew = skew;
      reg.operations = listing.operations;
      const operationIds = reg.operations.map(row => row.operation_id);
      const previous = reg.selected;
      if (focus && focus.target === reg.target) reg.selected = focus.operation_id;
      else if (!operationIds.includes(reg.selected)) {
        reg.selected = (reg.operations.find(row => row.state === 'pending_host_approval') || reg.operations[0] || {}).operation_id || '';
      }
      renderRegisteredList();
      if (!reg.selected) {
        reg.detail = null; reg.detailCountdowns = []; reg.detailKey = '';
        $('registered-detail').textContent = '작업을 선택하세요.'; $('registered-events').replaceChildren();
        $('registered-events-more').hidden = true;
      } else await loadRegisteredDetail(reg.selected !== previous || Boolean(focus));
      if (focus) {
        reg.focus = null;
        $('registered-ops').scrollIntoView({behavior: 'auto', block: 'start'});
      }
    } catch (error) {
      if (epoch !== reg.epoch) return;
      $('registered-ops').hidden = false;
      $('registered-availability').textContent = `등록 대상 작업을 불러오지 못했습니다: ${view.publicError(error)}`;
      $('registered-list').replaceChildren(); reg.operations = [];
    } finally {
      reg.busy = false; updateDecision();
      if (reg.pending) { reg.pending = false; queueMicrotask(refreshRegistered); }
      else if (epoch === reg.epoch) scheduleRegistered();
    }
  }
  async function decide(kind) {
    const operation = reg.detail;
    if (!operation || !view.canDecide(operation, registeredNow()) || reg.deciding) return;
    const confirmation = $('registered-confirmation').value.trim();
    const adminToken = $('registered-admin-token').value.trim();
    if (confirmation !== operation.operation_id) {
      $('registered-status').textContent = '선택한 작업 ID와 직접 입력한 ID가 다릅니다.';
      return;
    }
    if (!adminToken) {
      $('registered-status').textContent = '관리자 토큰을 다시 입력하세요.';
      return;
    }
    reg.deciding = true; updateDecision();
    const body = kind === 'approve'
      ? {operation_id: operation.operation_id, plan_digest: operation.plan_digest, source_digest: operation.source_digest,
        confirmation, admin_token: adminToken}
      : {operation_id: operation.operation_id, confirmation, admin_token: adminToken};
    try {
      const result = await api(`/api/registered-${kind}?${targetQuery()}`, {method: 'POST', body: JSON.stringify(body)});
      $('registered-status').textContent = kind === 'approve'
        ? `승인 영수증 발급: ${result.operation_id}. 실행은 ChatGPT에서 execute_host_operation 호출로만 진행됩니다.`
        : `거절 완료: ${operation.operation_id}`;
    } catch (error) {
      $('registered-status').textContent = `${kind === 'approve' ? '승인' : '거절'} 실패: ${view.publicError(error)}`;
    } finally {
      $('registered-confirmation').value = ''; $('registered-admin-token').value = '';
      reg.deciding = false; reg.cursor = 0;
      await refreshRegistered();
    }
  }
  $('registered-refresh').addEventListener('click', refreshRegistered);
  $('registered-target').addEventListener('change', () => {
    reg.target = $('registered-target').value; reg.selected = ''; reg.detail = null; reg.cursor = 0; refreshRegistered();
  });
  $('registered-events-more').addEventListener('click', () => loadRegisteredDetail(false));
  $('registered-decision-form').addEventListener('submit', event => event.preventDefault());
  $('registered-approve').addEventListener('click', () => decide('approve'));
  $('registered-reject').addEventListener('click', () => decide('reject'));
  $('host-scope-form').addEventListener('submit', async event => {
    event.preventDefault();
    const confirmation = $('host-scope-confirmation').value.trim();
    const admin_token = $('host-scope-token').value.trim();
    $('host-scope-confirmation').value = '';
    $('host-scope-token').value = '';
    try {
      const result = await api('/api/host-scope-enable?project=tossApp', {
        method: 'POST', body: JSON.stringify({confirmation, admin_token})
      });
      $('host-scope-status').textContent = result.enabled ?
        'Worker·DB 013 관리 범위 활성화 완료. 각 작업은 별도 승인이 필요합니다.' : '활성화되지 않았습니다.';
    } catch (error) {
      $('host-scope-status').textContent = '활성화 실패: ' + error.message;
    }
  });
  $('host-predecessor-form').addEventListener('submit', async event => {
    event.preventDefault();
    const confirmation = $('host-predecessor-confirmation').value.trim();
    const admin_token = $('host-predecessor-token').value.trim();
    $('host-predecessor-confirmation').value = '';
    $('host-predecessor-token').value = '';
    try {
      const result = await api('/api/host-predecessor-scope-enable?project=tossApp', {
        method: 'POST', body: JSON.stringify({confirmation, admin_token})
      });
      $('host-predecessor-status').textContent = result.enabled ?
        'DB 006~012 계획 권한 활성화 완료. 실제 적용에는 계획별 별도 승인이 필요합니다.' :
        '006~012 권한이 활성화되지 않았습니다.';
    } catch (error) {
      $('host-predecessor-status').textContent = '006~012 권한 활성화 실패: ' + error.message;
    }
  });
  $('host-refresh').addEventListener('click', refreshHostPlans);
  $('host-plan').addEventListener('change', () => { hostFocus = null; void showHostDetails(); });
  $('host-target-auth-form').addEventListener('submit', event => event.preventDefault());
  async function changeTargetAuthorization(enabled) {
    if (hostAuthorizationBusy || hostAuthorization?.supported !== true) return;
    const confirmation = $('host-target-auth-confirmation').value.trim();
    const admin_token = $('host-target-auth-token').value.trim();
    if (confirmation !== 'tossApp/tarororo-test') {
      $('host-target-auth-status').textContent = '대상 확인 문구가 tossApp/tarororo-test와 다릅니다.';
      return;
    }
    if (!admin_token) {
      $('host-target-auth-status').textContent = '관리자 토큰을 다시 입력하세요.';
      return;
    }
    const epoch = hostAuthorizationEpoch, projectEpoch = s.epoch;
    hostAuthorizationBusy = true; renderTargetAuthorization();
    try {
      const result = await api('/api/host-target-authorization?project=tossApp&target=tarororo-test', {
        method: 'POST', body: JSON.stringify({enabled, confirmation, admin_token})
      });
      if (epoch !== hostAuthorizationEpoch || projectEpoch !== s.epoch || s.project !== 'tossApp') return;
      if (!result || result.supported !== true || result.enabled !== enabled
          || result.project !== 'tossApp' || result.target !== 'tarororo-test') {
        throw new Error('권한 변경 결과가 요청과 일치하지 않습니다.');
      }
      hostAuthorization = result;
      $('host-target-auth-status').textContent = enabled
        ? '채팅 자동 실행 권한을 허용했습니다. 배포나 DB 변경은 아직 시작되지 않았습니다.'
        : '채팅 자동 실행 권한을 취소했습니다. 새 계획은 작업별 승인이 필요합니다.';
      await showHostDetails();
    } catch (error) {
      if (epoch === hostAuthorizationEpoch && projectEpoch === s.epoch) {
        $('host-target-auth-status').textContent = `대상 권한 ${enabled ? '허용' : '취소'} 실패: ${view.publicError(error)}`;
      }
    } finally {
      $('host-target-auth-confirmation').value = '';
      $('host-target-auth-token').value = '';
      hostAuthorizationBusy = false; renderTargetAuthorization();
    }
  }
  $('host-target-auth-enable').addEventListener('click', () => changeTargetAuthorization(true));
  $('host-target-auth-disable').addEventListener('click', () => changeTargetAuthorization(false));
  $('host-approve-form').addEventListener('submit', async event => {
    event.preventDefault();
    const plan = selectedHostPlan();
    if (!plan || (needsHostReview(plan)
        && (!hostReviewReady || hostReviewPlan !== plan.operation_id))) {
      $('host-status').textContent = '선택한 계획의 고정 소스 검토 자료를 먼저 확인하세요.';
      updateHostApproval(plan);
      return;
    }
    const confirmation = $('host-confirmation').value.trim();
    const adminToken = $('host-admin-token').value.trim();
    if (confirmation !== plan.operation_id) {
      $('host-status').textContent = '선택한 작업 ID와 직접 입력한 ID가 다릅니다.';
      return;
    }
    $('host-approve-submit').disabled = true;
    const projectEpoch = s.epoch;
    let decisionStatus = '';
    try {
      const approved = await api('/api/host-approve?project=tossApp', {
        method: 'POST',
        body: JSON.stringify({operation_id: plan.operation_id, plan_digest: plan.plan_digest,
          source_digest: plan.source_digest, confirmation, admin_token: adminToken})
      });
      if (approved.operation_id !== plan.operation_id || approved.approved !== true) {
        throw new Error('Approval response does not match the selected plan');
      }
      const executeTool = {api: 'execute_test_deploy', worker: 'execute_test_deploy',
        migration: 'execute_db_migration', schema_update: 'execute_db_migration',
        predecessor_migration: 'execute_predecessor_migration',
        schema014: 'execute_schema014_migration'}[plan.component];
      decisionStatus = `승인 완료: ${approved.operation_id}. ${executeTool
        ? '별도 실행 승인된 도구: ' + executeTool : '실행 도구를 채팅에서 확인하세요.'}`;
    } catch (error) {
      decisionStatus = `승인 결과를 확인하지 못했습니다: ${view.publicError(error)}`;
    } finally {
      $('host-confirmation').value = '';
      $('host-admin-token').value = '';
      await refreshHostPlans();
      if (s.connected && s.epoch === projectEpoch && s.project === 'tossApp') {
        $('host-status').textContent = decisionStatus;
      }
    }
  });

  async function login(token) {
    s.token = token; s.epoch++; const epoch = s.epoch;
    $('connection').textContent = '연결 중…';
    try {
      if (token) {
        await api('/api/session', {method: 'POST', body: JSON.stringify({remember: $('remember').checked})});
        if (epoch !== s.epoch) return;
        s.token = '';
      }
      await api('/api/status'); const data = await api('/api/projects');
      if (epoch !== s.epoch) return;
      s.connected = true;
      s.projects = data.projects; $('project').replaceChildren();
      for (const project of data.projects) {
        const option = document.createElement('option'); option.value = project.project;
        option.textContent = project.project; $('project').append(option);
      }
      $('token').value = ''; $('login').hidden = true; $('dashboard').hidden = false;
      $('dashboard').dataset.following = s.following ? 'true' : 'false';
      $('connection').textContent = '호스트 연결됨';
      const candidates = [hostAccessFocusLink?.project, reg.focus?.project, hostFocus?.project, rememberedProject(), data.recommended_project,
        data.projects[0]?.project];
      const selected = candidates.find(name => data.projects.some(project => project.project === name));
      if (selected) await switchProject(selected);
      await refreshHostPlans(); await refreshRegistered();
      window.dispatchEvent(new Event('personal-ops:authenticated'));
    } catch (error) { if (epoch === s.epoch) {
      s.token = ''; s.connected = false;
      notice(!token && error.status === 401 ? '' : error.message);
      $('connection').textContent = !token && error.status === 401 ? '로그인 필요' : '연결 실패';
    } }
  }
  $('login-form').addEventListener('submit', event => { event.preventDefault(); login($('token').value.trim()); });
  $('project').addEventListener('change', () => {
    if (s.saving || s.progressSaving || ((s.dirty || s.progressDirty) && !confirm('저장하지 않은 메모 또는 작업 보고를 버리고 레포를 바꿀까요?'))) {
      $('project').value = s.project; return;
    }
    switchProject($('project').value).then(refreshHostPlans).then(refreshRegistered);
  });
  $('memory').addEventListener('input', () => { s.dirty = true; $('memory-status').textContent = `저장하지 않은 변경 · 기준 v${s.revision}`; controls(); });
  $('save').addEventListener('click', async () => {
    const epoch = s.epoch, content = $('memory').value;
    s.saving = true; controls(); notice();
    try {
      const result = await api(`/api/memory?${query(s.project)}`, {method: 'PUT', body: JSON.stringify({content, expected_revision: s.revision})});
      if (epoch !== s.epoch) return;
      s.revision = result.revision; s.dirty = $('memory').value !== content;
      $('memory-status').textContent = `v${s.revision} 저장됨${s.dirty ? ' · 추가 변경 미저장' : ''}`;
    } catch (error) { if (epoch === s.epoch) notice(error.message); }
    finally { s.saving = false; controls(); }
  });
  $('reload-memory').addEventListener('click', async () => {
    if (s.saving || s.memoryLoading || (s.dirty && !confirm('입력한 내용을 버리고 최신 저장본을 읽을까요?'))) return;
    const epoch = s.epoch; s.memoryLoading = true; controls();
    try { await loadMemory(epoch); if (epoch === s.epoch) notice(); }
    catch (error) { if (epoch === s.epoch) notice(error.message); }
    finally { if (epoch === s.epoch) { s.memoryLoading = false; controls(); } }
  });
  for (const id of progressInputs) $(id).addEventListener('input', () => {
    s.progressDirty = true; $('progress-status').textContent = `저장하지 않은 변경 · 기준 v${s.progressRevision}`; controls();
  });
  $('progress-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!s.progressReady || s.progressSaving || s.progressLoading || !s.progressDirty) return;
    const lines = id => $(id).value.split('\n').map(value => value.trim()).filter(Boolean);
    const body = {title: $('progress-title').value.trim(), status: $('progress-state').value,
      summary: $('progress-summary').value, completed: lines('progress-completed'), next_steps: lines('progress-next'), expected_revision: s.progressRevision};
    const bytes = value => new TextEncoder().encode(value).length;
    if (!body.title || bytes(body.title) > 200 || bytes(body.summary) > 4096 ||
        [body.completed, body.next_steps].some(items => items.length > 20 || items.some(item => bytes(item) > 1000))) {
      notice('목표는 200 bytes, 설명은 4096 bytes, 각 목록은 20줄·한 줄 1000 bytes까지 저장할 수 있습니다.'); return;
    }
    const epoch = s.epoch; s.progressSaving = true; controls(); notice();
    try {
      const data = await api(`/api/progress?${query(s.project)}`, {method: 'PUT', body: JSON.stringify(body)});
      if (epoch !== s.epoch) return;
      progressDisplay(data, true); $('progress-status').textContent = `v${data.revision} 저장됨`;
    } catch (error) { if (epoch === s.epoch) notice(error.message); }
    finally { s.progressSaving = false; controls(); }
  });
  $('reload-progress').addEventListener('click', async () => {
    if (s.progressSaving || s.progressLoading || (s.progressDirty && !confirm('작업 보고 초안을 버리고 최신 저장본을 읽을까요?'))) return;
    const epoch = s.epoch; s.progressLoading = true; controls();
    try { await loadProgress(epoch); if (epoch === s.epoch) notice(); }
    catch (error) { if (epoch === s.epoch) notice(error.message); }
    finally { if (epoch === s.epoch) { s.progressLoading = false; controls(); } }
  });
  $('diff-mode').addEventListener('change', loadDiff);
  $('refresh-changes').addEventListener('click', () => refresh(true));
  $('make-handoff').addEventListener('click', async () => {
    if (s.handoffBusy) return;
    const epoch = s.epoch; s.handoffBusy = true; controls(); $('handoff-status').textContent = '현재 기록을 모으는 중…';
    try {
      const result = await api(`/api/handoff?${query(s.project)}`);
      if (epoch !== s.epoch) return;
      $('handoff').value = result.text;
      $('handoff-status').textContent = `${result.generated_at} 생성${s.dirty || s.progressDirty ? ' · 저장하지 않은 초안은 포함하지 않았습니다' : ''}`;
    } catch (error) { if (epoch === s.epoch) $('handoff-status').textContent = error.message; }
    finally { if (epoch === s.epoch) { s.handoffBusy = false; controls(); } }
  });
  $('copy-handoff').addEventListener('click', async () => {
    const epoch = s.epoch, text = $('handoff').value;
    if (!text) return;
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Use local copy');
      await navigator.clipboard.writeText(text);
      if (epoch === s.epoch) $('handoff-status').textContent = '복사했습니다. 새 채팅에서 personal-ops-tunnel을 선택하고 붙여 넣으세요.';
    } catch {
      if (epoch !== s.epoch) return;
      $('handoff').focus(); $('handoff').select();
      let copied = false;
      try { copied = document.execCommand('copy'); } catch { /* Manual selection remains available. */ }
      $('handoff-status').textContent = copied ? '복사했습니다. 새 채팅에 붙여 넣으세요.' : '요약을 선택했습니다. Ctrl+C 또는 휴대폰의 복사 메뉴를 사용하세요.';
    }
  });
  $('follow').addEventListener('click', () => {
    s.following = !s.following; $('follow').textContent = s.following ? '자동 갱신 일시정지' : '자동 갱신 재개';
    $('dashboard').dataset.following = s.following ? 'true' : 'false';
    workMap.setLive(mapShouldAnimate());
    if (s.following && !document.hidden) { refresh(); refreshRegistered(); } else clearPollingTimers();
  });
  $('refresh').addEventListener('click', () => refresh(true));
  $('logout').addEventListener('click', async () => {
    if (s.saving || s.progressSaving || ((s.dirty || s.progressDirty) && !confirm('저장하지 않은 내용을 버리고 연결을 해제할까요?'))) return;
    s.epoch++; s.jobEpoch++; s.diffEpoch++; s.connected = false; clearPollingTimers();
    s.mapJobs = []; s.mapEvents = []; s.mapRecords = []; s.mapHostExecutions = []; s.mapHostObservedAt = null;
    s.mapSummary = null; s.mapHostError = false;
    renderOperatingSummary();
    workMap.clear('', s.epoch);
    $('dashboard').inert = true; controls();
    try { await api('/api/session', {method: 'DELETE'}); }
    catch (error) {
      s.connected = true; s.memoryLoading = false; s.progressLoading = false; s.handoffBusy = false;
      if (!s.memoryReady) $('memory-status').textContent = '저장본 다시 읽기를 눌러 메모를 불러오세요.';
      if (!s.progressReady) $('progress-status').textContent = '저장본 다시 읽기를 눌러 작업 보고를 불러오세요.';
      notice(error.message); scheduleFast(); scheduleDetails(); scheduleRegistered(); return;
    }
    finally { $('dashboard').inert = false; controls(); }
    s.token = ''; s.project = ''; s.dirty = false; s.progressDirty = false; resetRegistered();
    $('dashboard').hidden = true; $('login').hidden = false;
    $('memory').value = ''; $('handoff').value = ''; $('log').textContent = '';
    $('connection').textContent = '연결 해제됨'; notice();
  });
  document.addEventListener('visibilitychange', () => {
    clearPollingTimers();
    workMap.setLive(mapShouldAnimate());
    if (!document.hidden && s.following) { refresh(); refreshRegistered(); }
  });
  window.addEventListener('beforeunload', event => { if (s.dirty || s.progressDirty) { event.preventDefault(); event.returnValue = ''; } });
  login(initialToken); initialToken = '';
})();
