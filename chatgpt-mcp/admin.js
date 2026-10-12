'use strict';
(() => {
  const fragment = new URLSearchParams(location.hash.slice(1));
  let initialToken = fragment.get('token') || '';
  if (location.hash) history.replaceState(null, '', location.pathname);
  const $ = id => document.getElementById(id);
  const s = {token: '', connected: false, project: '', job: '', offset: 0, revision: 0, dirty: false,
    epoch: 0, jobEpoch: 0, following: true, fastTimer: null, detailTimer: null,
    fastBusy: false, detailBusy: false, saving: false, memoryReady: false, memoryLoading: false,
    pendingFast: false, pendingDetail: false, pendingProgress: false, pendingGit: false, projects: [],
    progressRevision: 0, progressDirty: false, progressReady: false, progressLoading: false,
    progressSaving: false, displayedRevision: -1, gitTime: 0, file: '', diffEpoch: 0, handoffBusy: false,
    mapJobs: [], mapEvents: [], mapRecords: []};
  const progressInputs = ['progress-title', 'progress-state', 'progress-summary', 'progress-completed', 'progress-next'];
  const workMap = window.WorkDashboard.create($('work-map'), {onJobSelect: id => {
    selectJob(id);
    $('execution-details').scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start'});
  }});
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
    clearTimeout(s.fastTimer); clearTimeout(s.detailTimer);
  }
  function scheduleFast() {
    clearTimeout(s.fastTimer);
    if (pollingEnabled()) s.fastTimer = setTimeout(refreshFast, 1000);
  }
  function scheduleDetails() {
    clearTimeout(s.detailTimer);
    if (pollingEnabled()) s.detailTimer = setTimeout(refreshDetails, 5000);
  }
  async function refreshFast() {
    if (!s.connected || !s.project) return;
    if (s.fastBusy) { s.pendingFast = true; return; }
    clearTimeout(s.fastTimer); s.fastBusy = true;
    const epoch = s.epoch, project = s.project;
    const parts = [
      {path: '/api/jobs', render: renderJobs, error: 'checks', extraError: 'observed-status'},
      {path: '/api/activity', render: renderActivity, error: 'activity'}
    ];
    try {
      const results = await Promise.allSettled(parts.map(part => api(`${part.path}?${query(project)}`)));
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
      workMap.update({project, jobs: s.mapJobs, events: s.mapEvents, records: s.mapRecords, following: s.following,
        error: jobs.status === 'rejected' || activity.status === 'rejected' ? '일부 실행·호출 기록을 확인하지 못했습니다.' : ''});
      if (epoch === s.epoch) $('updated').textContent = `갱신 ${new Date().toLocaleTimeString()}`;
    } finally {
      s.fastBusy = false;
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
    s.handoffBusy = false; rememberProject(project);
    s.mapJobs = []; s.mapEvents = []; s.mapRecords = [];
    workMap.clear(project);
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
      $('connection').textContent = '호스트 연결됨';
      const candidates = [rememberedProject(), data.recommended_project, data.projects[0]?.project];
      const selected = candidates.find(name => data.projects.some(project => project.project === name));
      if (selected) await switchProject(selected);
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
    switchProject($('project').value);
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
    workMap.setLive(s.following);
    if (s.following && !document.hidden) refresh(); else clearPollingTimers();
  });
  $('refresh').addEventListener('click', () => refresh(true));
  $('logout').addEventListener('click', async () => {
    if (s.saving || s.progressSaving || ((s.dirty || s.progressDirty) && !confirm('저장하지 않은 내용을 버리고 연결을 해제할까요?'))) return;
    s.epoch++; s.jobEpoch++; s.diffEpoch++; s.connected = false; clearPollingTimers();
    s.mapJobs = []; s.mapEvents = []; s.mapRecords = [];
    workMap.clear();
    $('dashboard').inert = true; controls();
    try { await api('/api/session', {method: 'DELETE'}); }
    catch (error) {
      s.connected = true; s.memoryLoading = false; s.progressLoading = false; s.handoffBusy = false;
      if (!s.memoryReady) $('memory-status').textContent = '저장본 다시 읽기를 눌러 메모를 불러오세요.';
      if (!s.progressReady) $('progress-status').textContent = '저장본 다시 읽기를 눌러 작업 보고를 불러오세요.';
      notice(error.message); scheduleFast(); scheduleDetails(); return;
    }
    finally { $('dashboard').inert = false; controls(); }
    s.token = ''; s.project = ''; s.dirty = false; s.progressDirty = false;
    $('dashboard').hidden = true; $('login').hidden = false;
    $('memory').value = ''; $('handoff').value = ''; $('log').textContent = '';
    $('connection').textContent = '연결 해제됨'; notice();
  });
  document.addEventListener('visibilitychange', () => {
    clearPollingTimers();
    if (!document.hidden && s.following) refresh();
  });
  window.addEventListener('beforeunload', event => { if (s.dirty || s.progressDirty) { event.preventDefault(); event.returnValue = ''; } });
  login(initialToken); initialToken = '';
})();
