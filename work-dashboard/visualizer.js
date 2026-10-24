'use strict';
(() => {
  const roomInfo = {
    waiting: ['대기실', 'MCP·실행기 대기 · 실행 전 일감'], analysis: ['분석실', '조회·탐색 · 분석·설계'],
    coding: ['코딩실', '파일 생성 · 수정'], review: ['검토실', '검토·정리 · Git 기록'],
    execution: ['실행실', '명령 실행'], testing: ['검사실', '빌드 · 테스트'],
    done: ['기록 보관실', '반환 · 성공 일감 보관'], attention: ['확인 필요', '실패 · 중단 · 취소 일감']
  };
  const states = {started: '시작 기록', returned: '요청 반환', prepared: '준비됨 · 실행 전', queued: '대기', running: '실행 중',
    canceling: '취소 중', succeeded: '명령 성공', failed: '실패', timed_out: '시간 초과',
    canceled: '취소됨', interrupted: '중단됨', unknown: '종료 상태 미확인', idle: '대기 중', unobserved: '최근 범위 기록 없음'};
  const toolTitles = {read_file: '파일 읽기', list_dir: '목록 조회', find_file: '파일 찾기',
    search_for_pattern: '내용 검색', create_text_file: '파일 생성', replace_content: '코드 수정',
    start_command: '실행 요청', get_job: '실행 작업 조회', read_job_log: '실행 작업 로그',
    cancel_job: '실행 작업 취소 요청', list_jobs: '실행 작업 목록',
    git_status: 'Git 상태', git_diff: 'Git diff',
    read_project_memory: '메모 읽기', update_project_memory: '메모 저장',
    read_project_progress: '보고 읽기', update_project_progress: '보고 저장',
    list_execution_profiles: '명령 조회', prepare_git_operation: 'Git 계획',
    execute_git_operation: 'Git 실행', get_git_operation: 'Git 결과',
    get_runtime_status: '테스트 스택 상태', get_db_migration_status: 'DB 013 상태',
    get_predecessor_migration_status: 'DB 006~012 상태', get_schema014_migration_status: 'DB 014 상태',
    compare_deployment: '배포 상태 비교', prepare_test_deploy: '테스트 배포 계획',
    prepare_db_migration: 'DB 스키마 적용 계획', prepare_predecessor_migration: 'DB 006~012 적용 계획',
    prepare_schema014_migration: 'DB 014 적용 계획', execute_test_deploy: '테스트 배포 시작 요청',
    execute_db_migration: 'DB 스키마 적용 요청', execute_predecessor_migration: 'DB 006~012 적용 요청',
    execute_schema014_migration: 'DB 014 적용 요청', get_host_operation: '호스트 작업 상태',
    request_host_access: '호스트 접근 요청', get_host_access: '호스트 접근 상태',
    start_host_execution: '호스트 명령 시작 요청', get_host_execution: '호스트 명령 상태',
    read_host_execution_log: '호스트 명령 로그', cancel_host_execution: '호스트 명령 취소 요청',
    list_host_command_profiles: '호스트 명령 프로필', prepare_host_command: '호스트 명령 계획',
    execute_host_command: '호스트 명령 실행 요청', get_host_command: '호스트 명령 결과',
    cancel_host_command: '호스트 명령 중단 요청', list_agent_profiles: '에이전트 프로필',
    prepare_agent_task: '에이전트 작업 계획', start_agent_task: '에이전트 시작 요청',
    get_agent_task: '에이전트 작업 상태', cancel_agent_task: '에이전트 취소 요청',
    list_host_targets: '등록 호스트 대상', inspect_host_target: '호스트 대상 상태',
    prepare_host_operation: '호스트 작업 계획', execute_host_operation: '호스트 작업 시작 요청',
    read_host_operation_events: '호스트 작업 이벤트', prepare_runtime_rollout: '런타임 전환 계획',
    get_runtime_rollout: '런타임 전환 상태', execute_runtime_rollout: '런타임 전환 시작 요청',
    open_host_access_approval: '호스트 접근 승인 카드', update_host_access_approval: '호스트 접근 승인 옵션',
    confirm_host_access_approval: '호스트 접근 승인 확인', cancel_host_access_approval: '호스트 접근 승인 취소',
    open_schema014_approval: 'DB 014 승인 카드', confirm_schema014_approval: 'DB 014 승인 확인',
    cancel_schema014_approval: 'DB 014 승인 취소'};
  const active = new Set(['started', 'running', 'canceling']);
  const sessionTitles = {'Codex session active': '세션 활동 중', 'Codex session waiting': '세션 대기',
    'Codex session inactive': '활동 확인 대기'};
  const titleFor = actor => toolTitles[actor.tool] || (actor.kind === 'job' && actor.label === '직접 명령 실행'
    ? actionTitles[actor.action] || actor.label : sessionTitles[actor.label] || actor.label);
  const kindFor = item => ({call: item.sourceKind === 'mcp' ? 'MCP 호출' : '도구 호출', job: '명령 실행', agent: '에이전트 작업',
    mcp: 'MCP 서버', executor: '명령 실행기'}[item.kind] || '객체');
  const stateFor = item => item.agentActivity && item.state === 'running' ? '에이전트 활동 관측'
    : item.kind === 'agent' && item.state === 'succeeded' ? '작업 완료' : states[item.state];
  const observationTitle = owner => owner?.observationStatus?.kind === 'subagent' ? '서브에이전트 종료 미확인'
    : owner?.observationStatus?.kind === 'prompt' ? '요청 종료 미확인' : '';
  const actionTitles = {read: '책 읽기', search: '자료 탐색', reason: '추론 신호', code: '코드 작성', build: '빌드 조립',
    test: '검사', review: '변경 비교', execute: '명령 실행', commit: '커밋 기록', push: '전송', idle: '대기'};
  const taskGlyphs = {read: '[]', search: '?', reason: '*', code: '</>', build: '#', test: 'OK', review: '<>',
    execute: '>_', commit: 'G', push: '>>'};
  const roomTitle = id => roomInfo[id]?.[0] || '공용 복도';
  const localTime = time => time ? new Date(time).toLocaleString() : '기록 없음';
  const evidenceTitle = value => value === 'observed' ? '직접 관측' : '보고됨';
  const purposeEvidenceTitle = task => task?.workContext?.labelSource === 'reported'
    ? '작업 목적 보고됨' : task?.workContext?.labelSource === 'observed'
      ? '작업 목적 직접 관측' : '작업 목적 미수집';
  const usageScopes = {request: '요청 단위', turn: '턴 단위', session: '세션 단위', task: '작업 단위'};
  const sourceTitles = {'chatgpt': 'ChatGPT', 'codex-cli': 'Codex', 'claude-cli': 'Claude'};
  const roleLabels = {analysis: '분석가', research: '조사원', planning: '설계자', debugging: '디버거', review: '검토자', summary: '기록관'};
  const roleActions = {analysis: '분석 중', research: '검색 중', planning: '설계 중', debugging: '원인 추적 중', review: '검토 중', summary: '정리 중'};
  const roleGlyphs = {analysis: '?', research: 'WEB', planning: 'P', debugging: '!', review: '<>', summary: '='};
  const actionPhrases = {read: '읽는 중', search: '찾는 중', reason: '판단 중', code: '코드 작업', build: '빌드 중',
    test: '검사 중', review: '검토 중', execute: '명령 실행', commit: '커밋 기록', push: '전송 중', idle: '대기'};
  const actorName = actor => sourceTitles[actor?.sourceKind] || actor?.label || '담당 객체';
  const isHostCodex = task => task?.sourceKind === 'codex-cli' && Boolean(task.hostExecutionId);
  const isRawHostCodexTitle = task => isHostCodex(task) && /^Host Codex task [A-Za-z0-9_-]+$/.test(task.label || '');
  const missingHostPurpose = task => Boolean(task?.hostExecutionId)
    && task.workContext?.labelSource === 'unavailable';
  const hostReference = task => window.WorkDashboardModel.hostExecutionReference(task);
  const commandLabel = task => window.WorkDashboardModel.hostCommandLabel(task);
  const skillLabel = task => task?.skill?.name || task?.skill?.id || null;
  const skillDescription = task => task?.skill
    ? `${task.skill.name || task.skill.id} (${task.skill.id})` : null;
  const readableTaskTitle = task => missingHostPurpose(task)
    ? `작업 목적 미수집${hostReference(task) ? ' · ' + hostReference(task) : ''}`
    : task?.agentActivity ? '하위 에이전트 활동' : isRawHostCodexTitle(task) ? 'Codex 실행' : titleFor(task);
  const visualAction = task => (task?.hostExecutionId && task.hostActionKnown !== true)
    || (task?.action === 'code' && /^Host Codex task [A-Za-z0-9_-]+$/.test(task.label || ''))
    ? 'execute' : task?.action || 'idle';
  function readableAction(task) {
    if (!task) return '활동 미수신';
    if (task.hostExecutionId && task.hostActionKnown !== true) return '세부 행동 미수신';
    if (task.state === 'prepared') return '실행 전';
    if (task.state === 'queued') return '실행 대기';
    if (task.state === 'unknown') return '행동 상태 미확인';
    if (task.state === 'canceling') return '취소 처리 중';
    if (task.agentActivity) return '하위 에이전트 활동';
    if (active.has(task.state)) return actionPhrases[task.action] || actionTitles[task.action] || '활동 중';
    return `${actionTitles[task.action] || '활동'} 기록`;
  }
  function metadataValue(field) {
    return field ? `${field.value}${field.source === 'configuration' ? ' · 설정값' : ''} · ${evidenceTitle(field.provenance)} · ${localTime(field.time)}` : '미수신';
  }
  function usageValue(field) {
    if (!field) return '미수신';
    const labels = {input_tokens: '입력', output_tokens: '출력', reasoning_tokens: '추론', total_tokens: '합계'};
    const values = Object.entries(labels).filter(([key]) => Object.hasOwn(field.value, key))
      .map(([key, title]) => `${title} ${field.value[key].toLocaleString()}`);
    return `${usageScopes[field.scope]}${field.estimated ? ' · 추정' : ''} · ${values.join(' · ')} · ${evidenceTitle(field.provenance)} · ${localTime(field.time)}`;
  }

  function elapsedValue(task) {
    if (Number.isFinite(task?.result?.duration_ms) && task.result.duration_ms >= 0) {
      return `실행 시간 ${(task.result.duration_ms / 1000).toFixed(1)}초`;
    }
    const started = Date.parse(task?.invokedAt), observed = Date.parse(task?.observedAt);
    if (!Number.isFinite(started) || !Number.isFinite(observed) || observed < started) return null;
    return `마지막 관측까지 ${Math.floor((observed - started) / 1000).toLocaleString()}초`;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    if (tag === 'button') node.type = 'button';
    return node;
  }

  function brainIcon() {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.classList.add('wd-brain-icon'); svg.setAttribute('viewBox', '0 0 32 32');
    svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('focusable', 'false');
    const pixels = [[10, 4, 12, 4], [6, 8, 20, 4], [4, 12, 24, 12], [8, 24, 16, 4],
      [8, 10, 4, 4], [20, 10, 4, 4], [14, 8, 4, 8], [10, 18, 4, 4], [18, 18, 4, 4]];
    for (const [x, y, width, height] of pixels) {
      const rect = document.createElementNS(svg.namespaceURI, 'rect');
      rect.setAttribute('x', x); rect.setAttribute('y', y); rect.setAttribute('width', width); rect.setAttribute('height', height);
      svg.append(rect);
    }
    return svg;
  }

  function create(root, {onJobSelect = () => {}, onHostExecutionSelect = () => {}} = {}) {
    const {WORLD, ROOMS} = window.WorkDashboardMotion;
    const movement = window.WorkDashboardMotion.create();
    root.className = 'panel wd-map'; root.dataset.stale = 'false';
    let actors = [], tasks = [], sceneSprites = [], selected = '', selectedTask = '', selectedThinking = '', currentProject = '', roomFilter = '';
    let visibleSceneIds = new Set();
    let motion = true, following = true, stale = false, observed = false, destroyed = false;
    let frame = 0, idleTimer = 0, lastFrame = 0, clock = 0, zoom = 1, zoomMode = 'auto', seenTime = -Infinity;
    let activeCount = 0, movingScene = false;
    let previousSnapshot = null, lastQueryAt = 0, detailSignature = '', detailSelection = '', changeSignature = '';
    let resumeSnapshot = null, suspendedAt = 0, lastObservedAt = null, suspendedObservedAt = null;
    let recentChanges = [];
    const effects = new Set();
    let cameraTarget = '', talkChoice = '';
    let missionTaskId = '';
    let displayRoomValue = null, selectedMotionNote = null;
    const seen = new Set(), replayable = new Set(), tokens = new Map(), taskTokens = new Map(), roomNodes = new Map(), registry = new Map();
    const brainCards = new Map(), workItems = new Map(), thinkingTokens = new Map();
    const invocations = new Map(), invocationRanks = new Map(), evicted = new Map(), idleSprites = new Map();
    const historyRanks = new Map();
    const speeches = new Map();
    let speechTimer = 0, speechReady = false;
    let historySequence = 0;
    let callSequence = 0, observationOrder = new Map();
    const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');

    const heading = element('div', 'section-heading');
    const title = element('div');
    title.append(element('p', 'eyebrow', 'WORK GUILD'), element('h2', '', '업무 작업실'));
    const project = element('span', 'badge'); project.id = 'wd-project';
    const liveStatus = element('span', 'wd-live-status'); liveStatus.id = 'wd-live-status';
    liveStatus.title = '화면의 상태 조회 시각입니다. 각 작업의 관측 시각은 기록에서 확인할 수 있습니다.';
    const motionButton = element('button', '', '움직임 일시정지'); motionButton.id = 'wd-motion';
    heading.append(title, project, liveStatus);
    const mission = element('button', 'wd-mission'); mission.id = 'wd-mission';
    mission.append(element('small', '', '현재 임무'), element('strong', '', '수신된 작업 없음'), element('span', 'wd-mission-state'));
    mission.addEventListener('click', () => { if (missionTaskId) selectWork(missionTaskId); });
    const status = element('p', 'muted'); status.id = 'wd-status'; status.setAttribute('role', 'status');
    const pipeline = element('div', 'wd-pipeline'); pipeline.id = 'wd-pipeline';
    const pipelineValues = [];
    for (const [name, initial] of [['Agent / model', '모델·추론 상태 미수신'], ['Tunnel', '연결 상태 미수신'],
      ['MCP / CLI', '수집 객체 없음'], ['Work items', '일감 기록 없음']]) {
      const node = element('div', 'wd-node'), value = element('small', '', initial);
      node.append(element('strong', '', name), value); pipeline.append(node); pipelineValues.push(value);
    }
    const changesPanel = element('div', 'wd-changes');
    const changesTitle = element('strong', 'wd-changes-title', '최근 변화');
    const changesList = element('div', 'wd-changes-list'); changesList.id = 'wd-live-changes';
    changesPanel.append(changesTitle, changesList);
    const resumeNotice = element('p', 'wd-resume-notice'); resumeNotice.id = 'wd-resume-summary';
    resumeNotice.setAttribute('role', 'status'); resumeNotice.hidden = true;
    const workContext = element('div', 'wd-work-context');
    const brainPanel = element('section', 'wd-brain-panel');
    const brainHeading = element('div', 'wd-compact-heading');
    brainHeading.append(element('strong', '', '판단 두뇌'), element('small', '', '수신된 모델·활동만 표시'));
    const brains = element('div', 'wd-brains'); brains.id = 'wd-brains';
    brainPanel.append(brainHeading, brains);
    const boardPanel = element('section', 'wd-work-board-panel');
    const boardHeading = element('div', 'wd-compact-heading');
    boardHeading.append(element('strong', '', '지금 하는 일'), element('small', '', '현재 우선 · 완료는 이전 기록'));
    const workBoard = element('div', 'wd-work-board'); workBoard.id = 'wd-work-board';
    boardPanel.append(boardHeading, workBoard);
    const roleFlow = element('p', 'wd-role-flow');
    roleFlow.append(element('strong', '', '일반 역할 흐름'), document.createTextNode(' 판단 → 전달 → 실행 → 결과 안내'));
    workContext.append(brainPanel, boardPanel, roleFlow);
    const toolbar = element('div', 'wd-toolbar');
    const observation = element('span', 'wd-observation'); observation.id = 'wd-observation';
    const replayButton = element('button', '', '최근 활동 재생'); replayButton.id = 'wd-replay';
    replayButton.title = '확인한 시작·종료 기록의 이동을 다시 봅니다. 명령을 다시 실행하지 않습니다.';
    const zoomOut = element('button', 'wd-zoom', '−'); zoomOut.id = 'wd-zoom-out';
    const zoomIn = element('button', 'wd-zoom', '+'); zoomIn.id = 'wd-zoom-in';
    zoomOut.setAttribute('aria-label', '지도 축소'); zoomIn.setAttribute('aria-label', '지도 확대');
    const fitButton = element('button', '', '전체 보기'); fitButton.id = 'wd-fit';
    const zoomValue = element('span', 'wd-zoom-value'); zoomValue.id = 'wd-zoom-value';
    toolbar.append(observation, zoomOut, zoomValue, zoomIn, fitButton, motionButton, replayButton);

    const layout = element('div', 'wd-layout');
    const camera = element('div', 'wd-camera'); camera.id = 'wd-camera'; camera.tabIndex = 0;
    camera.setAttribute('aria-label', '작업실 지도. 방향키로 둘러보고 더하기·빼기로 확대·축소할 수 있습니다.');
    const sizer = element('div', 'wd-sizer');
    const stage = element('div', 'wd-stage'); stage.id = 'wd-world';
    stage.style.width = WORLD.width + 'px'; stage.style.height = WORLD.height + 'px';
    const mapCanvas = element('canvas', 'wd-scenery'); mapCanvas.setAttribute('aria-hidden', 'true');
    window.WorkDashboardPixels.drawMap(mapCanvas);
    const floor = element('div', 'wd-floor');
    for (const room of ROOMS) {
      const [name, description] = roomInfo[room.id];
      const node = element('section', 'wd-room wd-room-' + room.id); node.dataset.room = room.id;
      Object.assign(node.style, {left: room.x + 'px', top: room.y + 'px', width: room.width + 'px', height: room.height + 'px'});
      const top = element('div', 'wd-room-heading');
      const lamp = element('span', 'wd-room-lamp'); lamp.setAttribute('aria-hidden', 'true');
      const count = element('button', 'wd-room-count', '0'); count.setAttribute('aria-label', name + '의 표시 기록 보기');
      count.addEventListener('click', () => { roomFilter = room.id; selected = ''; selectedTask = ''; selectedThinking = ''; renderThinkingRoles(); showDetail(); });
      top.append(element('h3', '', name), lamp, count);
      const more = element('button', 'wd-more'); more.hidden = true;
      more.addEventListener('click', () => { roomFilter = room.id; selected = ''; selectedTask = ''; selectedThinking = ''; renderThinkingRoles(); showDetail(); });
      const occupancy = element('span', 'wd-room-occupancy', '인물 배정 0/6');
      node.append(top, element('p', 'wd-room-description', description), occupancy, more);
      floor.append(node); roomNodes.set(room.id, {node, count, more, occupancy});
    }
    const speechLayer = element('div', 'wd-speech-layer');
    stage.append(mapCanvas, floor, speechLayer); sizer.append(stage); camera.append(sizer);
    const inspector = element('aside', 'wd-inspector'); inspector.id = 'wd-detail'; inspector.tabIndex = -1;
    inspector.setAttribute('aria-label', '선택한 캐릭터와 작업');
    layout.append(camera, inspector);
    const hint = element('p', 'muted wd-hint', '지도를 끌거나 스크롤해 둘러보세요. 인물과 방의 개수를 누르면 기록을 확인할 수 있습니다.');
    const legend = element('p', 'muted wd-legend', '두뇌 · 같은 담당자의 분석·설계·검토 역할   WEB / FILE · 웹·파일 검색   청록 가방 · MCP   황금 가방 · 실행기   보라 가방 · 에이전트   문서 · 완료 기록');
    const note = element('p', 'muted wd-note', '호출된 인물만 차례로 입장해 일하고 대기실로 돌아갑니다. 완료 후 대기하는 객체도 포함되므로 화면의 인원은 현재 실행 중인 에이전트 수가 아닙니다. 종료 미확인 표시는 시작 후 새 상태를 확인하지 못한 경우이며 실행 중이라는 뜻이 아닙니다. 방은 여섯 자리이며, 정원이 차면 호출이 가장 오래된 인물이 퇴장합니다. 화면 밖에서도 실제 작업은 계속됩니다. 직접 관측·채팅 보고·과거 재생을 구분하며 ChatGPT 내부 추론과 외부 검색은 자동 수집되지 않습니다.');
    note.id = 'wd-map-note';
    const contextDetails = element('details', 'wd-context-details'); contextDetails.id = 'wd-context-details';
    contextDetails.append(element('summary', '', '작업 목록 · 모델 · 연결 상세'), pipeline, workContext);
    const help = element('details', 'wd-help');
    help.append(element('summary', '', '작업실 이용 안내'), hint, legend, note);
    root.replaceChildren(heading, mission, status, resumeNotice, toolbar, layout, changesPanel, contextDetails, help);

    const canMove = () => motion && following && !stale && !resumeSnapshot && !reducedMotion.matches && !document.hidden;
    const hasMotionActors = () => tokens.size > 0 || movement.snapshot().some(row => row.walking || row.replaying || row.departing)
      || [...thinkingTokens.values()].some(token => active.has(token.work?.state) && token.work.provenance === 'observed');
    function setZoom(value, mode = 'manual') {
      zoomMode = mode; zoom = Math.max(0.2, Math.min(2, value));
      stage.style.transform = `scale(${zoom})`;
      sizer.style.width = WORLD.width * zoom + 'px'; sizer.style.height = WORLD.height * zoom + 'px';
      zoomValue.textContent = Math.round(zoom * 100) + '%';
    }
    function fitMap() {
      const fitted = Math.min(camera.clientWidth / WORLD.width, camera.clientHeight / WORLD.height);
      setZoom(zoomMode === 'auto' && window.innerWidth <= 760 ? 0.8 : fitted, zoomMode);
      centerOnRecord(true);
    }
    function centerOnThinking(force = false) {
      const token = thinkingTokens.get(selectedThinking);
      if (!token || (!force && (zoomMode !== 'auto' || window.innerWidth > 760))) return;
      const position = movement.snapshot().find(row => row.id === selectedThinking);
      const room = ROOMS.find(row => row.id === token.sprite.room);
      const target = selectedThinking + ':' + token.sprite.room;
      if (!force && cameraTarget === target) return;
      cameraTarget = target;
      camera.scrollTo((position?.x ?? room.x + room.width / 2) * zoom - camera.clientWidth / 2,
        (position?.y ?? room.y + room.height / 2) * zoom - camera.clientHeight / 2);
    }
    function centerOnRecord(force = false) {
      if (selectedThinking) { centerOnThinking(force); return; }
      if ((!actors.length && !tasks.length) || (!force && (zoomMode !== 'auto' || window.innerWidth > 760))) return;
      const work = tasks.find(row => row.id === selectedTask);
      const chosen = sceneSprites.find(row => work ? row.taskId === work.id : row.id === selected)
        || work || actors.find(row => row.id === selected) || sceneSprites.find(row => active.has(row.state)) || actors[0];
      if (!chosen) return;
      const room = ROOMS.find(row => row.id === chosen.room);
      const target = chosen.room + ':' + chosen.id;
      if (!force && cameraTarget === target) return;
      cameraTarget = target;
      const position = movement.snapshot().find(row => row.id === chosen.id);
      camera.scrollTo((position?.x ?? room.x + room.width / 2) * zoom - camera.clientWidth / 2,
        (position?.y ?? room.y + room.height / 2) * zoom - camera.clientHeight / 2);
    }
    const observer = new ResizeObserver(() => { if (zoomMode !== 'manual') fitMap(); }); observer.observe(camera);
    zoomOut.addEventListener('click', () => setZoom(zoom / 1.25));
    zoomIn.addEventListener('click', () => setZoom(zoom * 1.25));
    fitButton.addEventListener('click', () => { zoomMode = 'fit'; fitMap(); camera.scrollTo(0, 0); });
    camera.addEventListener('keydown', event => {
      if (event.target !== camera) return;
      const directions = {ArrowLeft: [-100, 0], ArrowRight: [100, 0], ArrowUp: [0, -100], ArrowDown: [0, 100]};
      if (directions[event.key]) { event.preventDefault(); camera.scrollBy(...directions[event.key]); }
      else if (['+', '=', '-', '0'].includes(event.key)) {
        event.preventDefault();
        if (event.key === '0') fitButton.click(); else setZoom(zoom * (event.key === '-' ? 0.8 : 1.25));
      }
    });
    let drag = null;
    camera.addEventListener('pointerdown', event => {
      if (event.pointerType !== 'mouse' || event.button !== 0 || event.target.closest('button')) return;
      drag = {x: event.clientX, y: event.clientY, left: camera.scrollLeft, top: camera.scrollTop};
      camera.setPointerCapture(event.pointerId); camera.classList.add('wd-dragging');
    });
    camera.addEventListener('pointermove', event => {
      if (!drag) return;
      camera.scrollLeft = drag.left + drag.x - event.clientX; camera.scrollTop = drag.top + drag.y - event.clientY;
    });
    const stopDrag = () => { drag = null; camera.classList.remove('wd-dragging'); };
    camera.addEventListener('pointerup', stopDrag); camera.addEventListener('pointercancel', stopDrag);

    function brainState(actor, work) {
      if (!actor) return 'unobserved';
      if (stale) return 'stale';
      if (!following) return 'paused';
      if (actor.state === 'unknown') return 'unknown';
      if (actor.state === 'unobserved') return 'unobserved';
      if (actor.state === 'idle') return 'idle';
      return work?.action === 'reason' && active.has(work.state) && work.provenance === 'observed' ? 'reasoning' : 'active';
    }

    function brainStatus(state) {
      return {reasoning: '생각 중', active: '업무 수행 중', idle: '대기 중', paused: '관찰 일시정지', stale: '관측 지연',
        unknown: '활동 상태 미확인', unobserved: '활동 미수신'}[state] || '활동 미수신';
    }

    function thinkingTelemetry(actor, work) {
      return work ? work.telemetry || {} : actor?.telemetry || {};
    }

    function telemetrySummary(actor, work) {
      const telemetry = thinkingTelemetry(actor, work);
      const model = telemetry.model ? `${telemetry.model.value}${telemetry.model.source === 'configuration' ? ' · 설정값' : ''}` : '모델 미수신';
      const effort = telemetry.reasoningEffort
        ? `${telemetry.reasoningEffort.value}${telemetry.reasoningEffort.source === 'configuration' ? ' · 설정값' : ''}` : '추론 수준 미수신';
      return `${model} · ${effort}`;
    }

    function thinkingRows() {
      return actors.filter(actor => actor.kind === 'agent');
    }

    function thinkingWork(actor) {
      if (!actor || actor.placeholder) return null;
      const reasoning = tasks.filter(task => task.actorId === actor.id && task.action === 'reason' && active.has(task.state))
        .sort((left, right) => Number(right.provenance === 'observed') - Number(left.provenance === 'observed')
          || (Date.parse(right.time) || 0) - (Date.parse(left.time) || 0));
      return reasoning[0] || tasks.find(task => task.id === (actor.taskId || actor.lastTaskId)) || null;
    }

    function ensureBrainCard(key, actor) {
      let card = brainCards.get(key);
      if (card) return card;
      card = element(actor ? 'button' : 'article', 'wd-brain-card');
      card.append(brainIcon(), element('span', 'wd-brain-copy'));
      card.lastChild.append(element('strong', 'wd-brain-name'), element('span', 'wd-brain-state'),
        element('small', 'wd-brain-model'), element('small', 'wd-brain-work'));
      if (actor) card.addEventListener('click', () => select(key));
      brainCards.set(key, card); return card;
    }

    function renderBrains() {
      const rows = thinkingRows();
      const visible = new Set();
      rows.forEach((actor, index) => {
        const key = actor.id; visible.add(key);
        const card = ensureBrainCard(key, actor.placeholder ? null : actor);
        const work = thinkingWork(actor);
        const state = brainState(actor.placeholder ? null : actor, work);
        card.dataset.sourceKind = actor.sourceKind; card.dataset.state = state;
        if (actor.placeholder) delete card.dataset.actorId; else card.dataset.actorId = actor.id;
        card.classList.toggle('selected', !actor.placeholder && actor.id === selected);
        card.querySelector('.wd-brain-name').textContent = actor.placeholder ? 'ChatGPT' : actorName(actor);
        card.querySelector('.wd-brain-state').textContent = brainStatus(state);
        card.querySelector('.wd-brain-model').textContent = actor.placeholder ? '모델·활동 미수신' : telemetrySummary(actor, work);
        card.querySelector('.wd-brain-work').textContent = work
          ? `${readableTaskTitle(work)} · ${readableAction(work)}`
          : actor.placeholder ? '채팅 판단 역할 · 활동 미수신' : '현재 업무 미수신';
        card.setAttribute('aria-label', `${card.querySelector('.wd-brain-name').textContent} · ${brainStatus(state)} · ${card.querySelector('.wd-brain-work').textContent}`);
        if (brains.children[index] !== card) brains.insertBefore(card, brains.children[index] || null);
      });
      for (const [key, card] of brainCards) if (!visible.has(key)) { card.remove(); brainCards.delete(key); }
    }

    function roleTitle(sprite) {
      return sprite.role === 'research' ? sprite.searchScope === 'web' ? '웹검색' : sprite.searchScope === 'files'
        ? '파일 검색' : '자료 검색' : roleLabels[sprite.role] || '작업 역할';
    }

    function roleState(work) {
      if (stale) return 'stale';
      if (!following) return 'paused';
      if (!work || work.state === 'unknown') return 'unknown';
      if (!active.has(work.state)) return work.state;
      return work.action === 'reason' && work.provenance === 'observed' ? 'reasoning' : 'active';
    }

    function roleStatus(sprite, work) {
      const state = roleState(work);
      if (work?.skill) {
        const status = ['stale', 'paused', 'unknown'].includes(state) ? brainStatus(state)
          : `${sprite.state === 'idle' ? '대기 · ' : ''}${stateFor(work)}`;
        return `${evidenceTitle(work.skill.provenance)} · 스킬 ${skillLabel(work)} · ${status}`;
      }
      if (['stale', 'paused', 'unknown'].includes(state)) return brainStatus(state);
      if (sprite.state === 'idle' && work) return `대기 · ${stateFor(work)}`;
      if (!work || !active.has(work.state)) return work ? stateFor(work) : '활동 미수신';
      if (state === 'reasoning' && sprite.role === 'analysis') return '생각 중';
      return `${work.provenance === 'reported' ? '보고 · ' : ''}${roleActions[sprite.role] || readableAction(work)}`;
    }

    function ensureThinkingToken(sprite) {
      let token = thinkingTokens.get(sprite.id);
      if (token) { token.sprite = sprite; return token; }
      const button = element('button', 'wd-thinking-character wd-role-character');
      const icon = brainIcon(), mark = element('span', 'wd-role-mark'); mark.setAttribute('aria-hidden', 'true');
      const avatar = element('canvas', 'wd-thinking-avatar'); avatar.setAttribute('aria-hidden', 'true');
      const skillMark = element('span', 'wd-skill-mark', '✦'); skillMark.setAttribute('aria-hidden', 'true');
      const copy = element('span', 'wd-thinking-copy');
      copy.append(element('strong', 'wd-thinking-role'), element('small', 'wd-thinking-owner'), element('span', 'wd-thinking-state'),
        element('small', 'wd-thinking-model'), element('small', 'wd-thinking-work'));
      button.append(icon, mark, avatar, skillMark, copy);
      button.addEventListener('click', () => selectThinking(sprite.id));
      stage.append(button);
      token = {button, avatar, sprite, work: null, state: 'unobserved', poseKey: ''};
      thinkingTokens.set(sprite.id, token); return token;
    }

    function renderThinkingRoles() {
      for (const [key, token] of thinkingTokens) {
        const sprite = token.sprite;
        const owner = actors.find(row => row.id === sprite.ownerId);
        const work = tasks.find(row => row.id === sprite.taskId) || token.work;
        token.work = work; token.state = roleState(work);
        token.button.dataset.sourceKind = sprite.sourceKind; token.button.dataset.state = token.state;
        token.button.dataset.ownerId = sprite.ownerId; token.button.dataset.role = sprite.role;
        token.button.dataset.room = sprite.room; token.button.dataset.kind = sprite.kind;
        token.button.dataset.taskId = sprite.taskId; token.button.dataset.provenance = work?.provenance || 'reported';
        if (sprite.searchScope) token.button.dataset.searchScope = sprite.searchScope; else delete token.button.dataset.searchScope;
        token.button.classList.toggle('selected', key === selectedThinking || Boolean(selectedTask && selectedTask === sprite.taskId));
        token.button.querySelector('.wd-role-mark').textContent = sprite.role === 'research'
          ? sprite.searchScope === 'web' ? 'WEB' : sprite.searchScope === 'files' ? 'FILE' : '?' : roleGlyphs[sprite.role];
        token.button.querySelector('.wd-thinking-role').textContent = skillLabel(work) || roleTitle(sprite);
        token.button.querySelector('.wd-thinking-owner').textContent = actorName(owner);
        token.button.querySelector('.wd-thinking-state').textContent = roleStatus(sprite, work);
        token.button.querySelector('.wd-thinking-model').textContent = telemetrySummary(owner, work);
        token.button.querySelector('.wd-thinking-work').textContent = work ? readableTaskTitle(work) : '활동 미수신';
        token.button.setAttribute('aria-label', `${actorName(owner)} · ${roleTitle(sprite)} · ${roleStatus(sprite, work)}`);
        token.button.title = `${actorName(owner)} · ${roleTitle(sprite)} · ${roleStatus(sprite, work)} · ${telemetrySummary(owner, work)}`;
      }
    }

    function ensureWorkItem(id) {
      let item = workItems.get(id);
      if (item) return item;
      item = element('button', 'wd-work-item'); item.dataset.taskId = id;
      item.append(element('span', 'wd-work-person'), element('span', 'wd-work-action'),
        element('strong', 'wd-work-title'), element('small', 'wd-work-state'), element('small', 'wd-work-parent'));
      item.addEventListener('click', () => selectWork(id));
      workItems.set(id, item); return item;
    }

    function boardPriority(task) {
      if ((task.sessionActivity || task.agentActivity) && (active.has(task.state) || task.state === 'queued')) return 3;
      if (active.has(task.state)) return 0;
      if (task.state === 'prepared') return 1;
      if (task.state === 'queued') return 2;
      if (task.state === 'unknown') return 4;
      return 5;
    }

    function boardStatus(task) {
      if (stale) return `마지막 확인 · ${stateFor(task)}`;
      if (resumeSnapshot) return `복귀 동기화 중 · ${stateFor(task)}`;
      if (!following) return `관찰 일시정지 · ${stateFor(task)}`;
      if (active.has(task.state)) return `현재 · ${stateFor(task)}`;
      if (task.state === 'prepared') return `실행 전 · ${stateFor(task)}`;
      if (task.state === 'queued') return `대기 · ${stateFor(task)}`;
      if (task.state === 'unknown') return `상태 미확인 · ${stateFor(task)}`;
      return `이전 기록 · ${stateFor(task)}`;
    }

    function renderWorkBoard() {
      const rows = [...tasks].sort((left, right) => boardPriority(left) - boardPriority(right)
        || (Date.parse(right.time) || 0) - (Date.parse(left.time) || 0) || left.id.localeCompare(right.id)).slice(0, 4);
      if (selectedTask && !rows.some(task => task.id === selectedTask)) {
        const chosen = tasks.find(task => task.id === selectedTask);
        if (chosen) rows.splice(Math.max(0, rows.length - 1), 1, chosen);
      }
      const visible = new Set();
      rows.forEach((task, index) => {
        visible.add(task.id);
        const item = ensureWorkItem(task.id);
        const actor = actors.find(row => row.id === task.actorId);
        const parent = task.parentTaskId ? tasks.find(row => row.id === task.parentTaskId) : null;
        item.dataset.state = task.state; item.dataset.provenance = task.provenance || 'reported';
        item.classList.toggle('selected', task.id === selectedTask);
        item.querySelector('.wd-work-person').textContent = actorName(actor);
        const actionText = task.skill
          ? `스킬 · ${skillLabel(task)} · ${evidenceTitle(task.skill.provenance)}` : readableAction(task);
        item.querySelector('.wd-work-action').textContent = actionText;
        item.querySelector('.wd-work-title').textContent = readableTaskTitle(task);
        item.querySelector('.wd-work-state').textContent = boardStatus(task);
        const relation = item.querySelector('.wd-work-parent');
        relation.textContent = parent ? `상위 업무 · ${readableTaskTitle(parent)}` : '';
        relation.hidden = !parent;
        item.setAttribute('aria-label', `${actorName(actor)} · ${actionText} · ${readableTaskTitle(task)} · ${stateFor(task)}`);
        if (workBoard.children[index] !== item) workBoard.insertBefore(item, workBoard.children[index] || null);
      });
      for (const [id, item] of workItems) if (!visible.has(id)) { item.remove(); workItems.delete(id); }
      if (!rows.length && !workBoard.querySelector('.wd-work-empty')) {
        workBoard.append(element('p', 'wd-work-empty', '현재 수신된 업무 없음'));
      } else if (rows.length) workBoard.querySelector('.wd-work-empty')?.remove();
    }

    function renderWorkContext() {
      renderBrains(); renderWorkBoard();
      const rows = [...tasks].sort((a, b) => boardPriority(a) - boardPriority(b)
        || Date.parse(b.time) - Date.parse(a.time));
      const work = rows[0]; missionTaskId = work?.id || ''; mission.disabled = !work;
      mission.querySelector('small').textContent = work?.sessionActivity ? '세션 상태'
        : work?.agentActivity ? '하위 에이전트 상태'
        : work && active.has(work.state) ? '현재 임무'
          : ['prepared', 'queued'].includes(work?.state) ? '대기 중인 업무' : '최근 작업 기록';
      mission.querySelector('strong').textContent = work ? readableTaskTitle(work) : '수신된 작업 없음';
      mission.querySelector('.wd-mission-state').textContent = work ? `${actorName(actors.find(row => row.id === work.actorId))} · ${boardStatus(work)}` : '캐릭터는 실제 호출 후 입장합니다';
    }

    function jobBadge(owner) {
      const job = window.WorkDashboardModel.characterJob(owner);
      const badge = element('span', 'wd-job-badge', `${job.mark} ${job.label}`);
      badge.dataset.job = job.id; badge.title = `${job.equipment} · 객체 종류에 따른 표시 직업`;
      return badge;
    }

    function appendTalk(owner, work) {
      const panel = element('section', 'wd-talk'); panel.setAttribute('aria-label', '기록으로 말 걸기');
      panel.append(element('p', 'wd-talk-note', '기록으로 말 걸기 · 추가 AI 호출 없음'));
      const answer = element('p', 'wd-talk-answer'); answer.id = 'wd-talk-answer'; answer.setAttribute('role', 'status');
      const buttons = element('div', 'wd-talk-buttons');
      const reply = question => {
        talkChoice = question;
        answer.textContent = window.WorkDashboardModel.recordedAnswer({actor: owner, task: work, tasks, question,
          stale: stale || Boolean(resumeSnapshot), paused: !following, archived: Boolean(work && !tasks.some(row => row.id === work.id))});
        for (const button of buttons.children) button.setAttribute('aria-pressed', String(button.dataset.question === question));
        if (question === 'history') {
          const records = inspector.querySelector('#wd-related-records');
          if (records) records.open = true;
        }
      };
      for (const [question, label] of [['current', '지금 뭐 해?'], ['stop', '왜 멈췄어?'], ['result', '마지막 결과는?'], ['history', '관련 기록 보여줘']]) {
        const button = element('button', '', label); button.dataset.question = question;
        button.setAttribute('aria-pressed', 'false'); button.addEventListener('click', () => reply(question)); buttons.append(button);
      }
      panel.append(buttons, answer); inspector.append(panel);
      if (talkChoice) reply(talkChoice);
    }

    function appendLog(work) {
      if (!work?.hostExecutionId && !work?.jobId) return;
      const open = element('button', 'wd-open-log', work.hostExecutionId ? '호스트 실행 상세 열기' : '원본 출력 열기');
      open.id = 'wd-open-log';
      open.addEventListener('click', () => work.hostExecutionId ? onHostExecutionSelect(work.hostExecutionId) : onJobSelect(work.jobId));
      inspector.append(open);
    }

    function appendRelated(owner, work) {
      const rows = window.WorkDashboardModel.recentTasks(tasks.filter(row => row.actorId === (owner?.id || work?.actorId)));
      const records = element('details', 'wd-related-records'); records.id = 'wd-related-records';
      records.open = talkChoice === 'history';
      records.append(element('summary', '', `관련 기록 · ${rows.length}개`));
      for (const row of rows.filter(row => row.id !== work?.id).slice(0, 8)) records.append(taskLink(row, stateFor(row)));
      if (rows.length === 1 && rows[0].id === work?.id) records.append(element('p', 'muted', '선택한 작업은 위 요약에 표시됩니다. 다른 일감은 수신되지 않았습니다.'));
      if (rows.length > 8) records.append(element('p', 'muted', '최신 8개 표시 · 방의 기록 수를 누르면 전체 조회 목록을 볼 수 있습니다.'));
      if (!rows.length) records.append(element('p', 'muted', '현재 조회 범위에 연결된 일감이 없습니다.'));
      inspector.append(records);
    }

    function appendNativeActivity(owner, work) {
      if (!['codex-cli', 'claude-cli'].includes(owner?.sourceKind)) return;
      const rows = window.WorkDashboardModel.recentTasks(tasks.filter(task =>
        task.actorId === owner.id && task.kind === 'call' && !task.sessionActivity && !task.agentActivity));
      if (!rows.length) return;
      const pending = task => active.has(task.state) || task.state === 'queued';
      const current = rows.filter(pending).slice(0, 3);
      const recent = rows.filter(task => !pending(task)).slice(0, 6 - current.length);
      const panel = element('section', 'wd-detail-summary wd-native-activity');
      const heading = owner.sourceKind === 'claude-cli' ? '최근 Claude 호출' : '최근 터미널 활동';
      panel.setAttribute('aria-label', heading);
      panel.append(element('small', 'wd-summary-heading', heading));
      const currentTitle = stale || resumeSnapshot || !following ? '마지막 확인 호출' : '현재 호출';
      for (const [id, title, entries, empty] of [
        ['current', currentTitle, current, '현재 호출 기록 없음'],
        ['recent', '최근 결과', recent, '종료·미확인 기록 없음'],
      ]) {
        const group = element('section', 'wd-native-group');
        group.dataset.nativeGroup = id;
        group.append(element('h4', '', title));
        if (!entries.length) group.append(element('p', 'wd-native-meta', empty));
        const list = element('ol', 'wd-native-history');
        for (const task of entries) {
          const entry = element('li', 'wd-native-entry');
          entry.dataset.taskId = task.id; entry.dataset.state = task.state;
          const title = readableTaskTitle(task);
          const label = task.tool ? `${task.tool} · ${title}` : title;
          const button = taskLink(task, '작업 상세');
          button.textContent = label; button.setAttribute('aria-label', `작업 상세 · ${label}`);
          if (task.id === work?.id) button.setAttribute('aria-current', 'true');
          const state = element('p', 'wd-native-state', task.state === 'queued' ? '응답 대기' : stateFor(task));
          if (Number.isInteger(task.result?.exit_code)) {
            state.append(' · ', element('span', 'wd-summary-numeric', `종료 코드 ${task.result.exit_code}`));
          }
          const time = element('time', '', localTime(task.time));
          time.dateTime = task.time;
          const metadata = element('p', 'wd-native-meta', `${evidenceTitle(task.provenance)} · `);
          metadata.append(time);
          entry.append(button, state, metadata); list.append(entry);
        }
        group.append(list); panel.append(group);
      }
      if (rows.length > current.length + recent.length) panel.append(element('p', 'wd-native-meta',
        `현재·최근 ${current.length + recent.length}개 표시 · 다른 호출은 관련 기록에서 확인`));
      inspector.append(panel);
    }

    function revealDetail() {
      if (window.innerWidth <= 760) {
        inspector.focus({preventScroll: true}); inspector.scrollIntoView({block: 'nearest'});
      }
    }

    function taskLink(task, relation) {
      const title = missingHostPurpose(task)
        ? `${hostReference(task) || '이전 실행'} · ${commandLabel(task)}` : readableTaskTitle(task);
      const button = element('button', 'wd-task-link', `${relation} · ${title}`);
      button.dataset.taskId = task.id;
      button.addEventListener('click', () => selectWork(task.id));
      return button;
    }

    function taskDisplayRoom(task) {
      return sceneSprites.find(row => row.taskId === task.id && !['idle', 'unobserved'].includes(row.state))?.room || task.room;
    }

    function workSummary(work, owner) {
      const summary = element('section', 'wd-detail-summary wd-work-summary');
      const name = actorName(owner);
      const ownerLabel = owner?.label && owner.label !== name ? `${name} · ${owner.label}` : name;
      summary.setAttribute('aria-label', '작업 요약');
      summary.append(element('small', 'wd-summary-heading', '작업 요약'),
        element('strong', 'wd-summary-purpose', readableTaskTitle(work)),
        element('p', '', `${ownerLabel} · ${boardStatus(work)}`));
      if (work.hostExecutionId) {
        if (missingHostPurpose(work)) summary.append(element('p', 'wd-summary-purpose-note',
          '이 실행을 요청한 업무명은 당시 수집되지 않았습니다. 실행 번호로 다른 기록과 구분합니다.'));
        summary.append(element('p', 'wd-summary-command', `명령 종류 · ${commandLabel(work)}`),
          element('p', 'wd-summary-phase', `관측 단계 · ${window.WorkDashboardModel.hostPhaseLabel(work.hostPhase)}${
            work.hostActionKnown === true ? ' · ' + (actionTitles[work.action] || work.action) : ''}`));
        if (work.hostActionKnown !== true) summary.append(element('p', 'wd-summary-action', '세부 행동 미수신'));
      } else {
        summary.append(element('p', 'wd-summary-action', `${readableAction(work)}${work.tool ? ' · ' + work.tool : ''}`));
      }
      if (work.skill) summary.append(element('p', 'wd-summary-skill',
        `스킬 · ${skillDescription(work)} · ${stateFor(work)} · ${evidenceTitle(work.skill.provenance)}`));
      const results = {
        succeeded: '성공 상태가 기록됐습니다.', failed: '실패 상태가 기록됐습니다.',
        returned: work.skill
          ? '스킬 호출이 반환됐습니다. 스킬이 지시한 워크플로 전체의 완료가 확인된 것은 아닙니다.'
          : '도구 요청이 반환됐습니다. 실행 성공 여부는 별도 실행 결과에서 확인하세요.',
        timed_out: '제한 시간을 넘긴 것으로 기록됐습니다.', canceled: '취소 상태가 기록됐습니다.',
        interrupted: '중단 상태가 기록됐습니다.', unknown: '완료 결과를 확인하지 못했습니다.',
        queued: '실행 대기 중인 일감입니다.', prepared: '실행 전 준비 상태입니다.',
        running: '실행 중으로 관측됐으며 완료 결과는 아직 없습니다.',
        started: '작업 시작이 관측됐으며 완료 결과는 아직 없습니다.', canceling: '취소 처리 중으로 관측됐습니다.',
      };
      const archived = !tasks.some(task => task.id === work.id);
      const hostResult = work.hostExecutionId ? window.WorkDashboardModel.hostResultSummary(work) : null;
      const stateResult = hostResult || (work.agentActivity && active.has(work.state)
        ? '하위 에이전트 활동이 관측됐습니다. 개별 도구 실행과 내부 추론 상태는 이 신호만으로 알 수 없습니다.'
        : work.agentActivity && work.state === 'returned'
          ? '하위 에이전트의 반환 상태가 관측됐습니다. 전체 업무의 성공을 뜻하지 않습니다.'
          : results[work.state] || '');
      const explanation = archived ? `현재 조회 범위를 벗어난 이전 기록입니다. ${stateResult}`
        : (stale || resumeSnapshot || !following) && active.has(work.state)
          ? '마지막으로 확인한 실행 상태입니다. 최신 관측 결과를 기다리고 있습니다.'
          : stateResult || '추가 실행 결과는 수신되지 않았습니다.';
      summary.append(element('p', 'wd-summary-result', explanation));
      const result = work.result || {}, numeric = [];
      if (Number.isInteger(result.exit_code)) numeric.push(`종료 코드 ${result.exit_code}`);
      if (Number.isFinite(result.duration_ms) && result.duration_ms >= 0) numeric.push(`실행 시간 ${(result.duration_ms / 1000).toFixed(1)}초`);
      for (const [key, label] of [['stdout_bytes', '표준 출력'], ['stderr_bytes', '오류 출력']]) {
        if (Number.isSafeInteger(result[key]) && result[key] >= 0) numeric.push(`${label} ${result[key].toLocaleString()}B`);
      }
      if (numeric.length) summary.append(element('p', 'wd-summary-numeric', numeric.join(' · ')));
      if (work.hostCode) summary.append(element('p', 'wd-summary-code', `결과 코드 · ${work.hostCode}`));
      const elapsed = elapsedValue(work);
      summary.append(element('p', 'wd-summary-evidence', `${purposeEvidenceTitle(work)} · 실행 ${evidenceTitle(work.provenance)} · ${elapsed ? elapsed + ' · ' : ''}마지막 관측 ${localTime(work.observedAt || work.time)}`));
      return summary;
    }

    function showDetail() {
      const signature = JSON.stringify([selected, selectedTask, selectedThinking, roomFilter, actors, tasks, sceneSprites, stale, following, Boolean(resumeSnapshot)]);
      if (signature === detailSignature) return;
      detailSignature = signature;
      const selection = JSON.stringify([currentProject, selected, selectedTask, selectedThinking, roomFilter]);
      const sameSelection = detailSelection === selection; detailSelection = selection;
      const focusKey = node => node ? JSON.stringify([node.tagName, node.id, node.dataset.question,
        node.dataset.taskId, node.dataset.actorId, node.dataset.roleId,
        node.closest('details')?.className, node.parentElement?.className]) : '';
      const focused = inspector.contains(document.activeElement) && document.activeElement !== inspector
        ? focusKey(document.activeElement) : '';
      const detailsState = new Map([...inspector.querySelectorAll('details')].map(node => [node.className, node.open]));
      const previousScroll = inspector.scrollTop;
      const restoreInspector = () => {
        if (sameSelection) {
          for (const node of inspector.querySelectorAll('details')) {
            if (detailsState.has(node.className)) node.open = detailsState.get(node.className);
          }
          inspector.scrollTop = previousScroll;
        }
        if (focused) {
          const target = sameSelection && [...inspector.querySelectorAll('button, summary')].find(node => focusKey(node) === focused);
          (target || inspector).focus({preventScroll: true});
        }
      };
      inspector.replaceChildren(); displayRoomValue = null; selectedMotionNote = null;
      const back = element('button', 'wd-back-map', '지도로 돌아가기'); back.id = 'wd-back-map';
      back.addEventListener('click', () => { camera.focus({preventScroll: true}); camera.scrollIntoView({block: 'nearest'}); });
      inspector.append(back);
      if (selectedThinking) {
        const token = thinkingTokens.get(selectedThinking);
        const sprite = token?.sprite || sceneSprites.find(row => row.id === selectedThinking);
        const owner = actors.find(row => row.id === sprite?.ownerId);
        const work = tasks.find(row => row.id === sprite?.taskId) || token?.work;
        inspector.append(element('p', 'eyebrow', 'WORK ROLE'), element('h3', '', `사고 역할 · ${sprite ? roleTitle(sprite) : '기록'}`));
        const summary = element('section', 'wd-detail-summary');
        summary.append(element('strong', '', `${actorName(owner)} · ${sprite ? roleStatus(sprite, work) : '기록'}`),
          element('p', '', work ? `${readableTaskTitle(work)} · ${evidenceTitle(work.provenance)}` : '활동 미수신'));
        inspector.append(work ? workSummary(work, owner) : summary, element('p', 'muted', owner
          ? '같은 담당자의 작업 역할입니다. 별도 모델이나 작업자를 추가로 실행하지 않습니다.'
          : 'ChatGPT의 사고 역할 자리입니다. 현재 모델과 활동 신호는 미수신입니다.'));
        if (owner) {
          inspector.append(jobBadge(owner)); appendTalk(owner, work); appendLog(work); appendRelated(owner, work);
          const telemetry = thinkingTelemetry(owner, work);
          const fields = element('dl', 'wd-fields');
          const entries = [['역할 상태', roleStatus(sprite, work)], ['모델', metadataValue(telemetry.model)],
            ['추론 수준', metadataValue(telemetry.reasoningEffort)], ['토큰', usageValue(telemetry.usage)],
            ['기록 출처', work ? evidenceTitle(work.provenance) : '미수신'], ['객체 식별자', owner.id]];
          if (work) entries.push(['연결 업무', readableTaskTitle(work)], ['일감 식별자', work.hostExecutionId || work.jobId || work.id]);
          for (const [key, value] of entries) fields.append(element('dt', '', key), element('dd', '', value));
          const technical = element('details', 'wd-detail-technical');
          technical.append(element('summary', '', '기술 세부 정보'), fields); inspector.append(technical);
          if (work) inspector.append(taskLink(work, '실제 업무 보기'));
        }
        restoreInspector(); paint(); return;
      }
      const actor = actors.find(row => row.id === selected);
      const work = tasks.find(row => row.id === (selectedTask || actor?.taskId || actor?.lastTaskId))
        || (!selectedTask ? tokens.get(selected)?.work : null);
      if (!actor && !work) {
        inspector.append(element('p', 'eyebrow', 'WORK INSPECTOR'), element('h3', '', roomFilter ? roomTitle(roomFilter) : '객체와 일감'));
        const list = roomFilter ? window.WorkDashboardModel.recentTasks(
          tasks.filter(row => taskDisplayRoom(row) === roomFilter), {order: historyRanks}) : [];
        inspector.append(element('p', 'muted', list.length ? '확인할 일감을 선택하세요.' : '객체나 보관된 일감을 선택하면 작업과 원본 출력을 확인할 수 있습니다.'));
        for (const row of actors.filter(row => !roomFilter || row.room === roomFilter)) {
          const button = element('button', 'wd-detail-item wd-entity-item', `${row.label} · ${states[row.state]}`);
          button.dataset.actorId = row.id;
          button.addEventListener('click', () => select(row.id)); inspector.append(button);
        }
        for (const row of sceneSprites.filter(row => row.displayType === 'role' && (!roomFilter || row.room === roomFilter))) {
          const owner = actors.find(actor => actor.id === row.ownerId);
          const button = element('button', 'wd-detail-item wd-role-item', `${roleTitle(row)} · ${actorName(owner)}`);
          button.dataset.roleId = row.id;
          button.addEventListener('click', () => selectThinking(row.id)); inspector.append(button);
        }
        for (const row of list) {
          const button = element('button', 'wd-detail-item', `${kindFor(row)} · ${readableTaskTitle(row)} · ${stateFor(row)}`);
          button.dataset.taskId = row.id;
          button.addEventListener('click', () => selectWork(row.id)); inspector.append(button);
        }
        restoreInspector(); return;
      }
      inspector.append(element('p', 'eyebrow', kindFor(actor || work)), element('h3', '', actor ? actorName(actor) : readableTaskTitle(work)));
      const owner = actor || actors.find(row => row.id === work?.actorId);
      const summary = work ? workSummary(work, owner) : element('section', 'wd-detail-summary');
      if (!work) summary.append(element('strong', '', actorName(owner)),
        element('p', '', `${actorName(owner)} · ${stateFor(owner)} · 현재 업무 ${owner?.taskId ? '관측됨' : '미수신'}`));
      inspector.append(jobBadge(owner), summary);
      if (observationTitle(owner)) {
        const observation = element('section', 'wd-detail-summary wd-detail-observation');
        observation.append(element('strong', '', observationTitle(owner)),
          element('p', '', `시작 기록 · ${localTime(owner.observationStatus.time)}`),
          element('p', '', '종료 기록은 최근 수신 범위에서 확인되지 않았습니다. 현재 실행 중인 에이전트 수나 작업 중단을 뜻하지 않습니다.'));
        const lifecycle = tasks.find(task => task.id === owner.observationStatus.taskId);
        if (lifecycle) observation.append(taskLink(lifecycle, '시작 기록 확인'));
        inspector.append(observation);
      }
      appendNativeActivity(owner, work);
      appendTalk(owner, work); appendLog(work); appendRelated(owner, work);
      if (work) {
        const links = element('div', 'wd-task-links');
        const parent = work.parentTaskId ? tasks.find(row => row.id === work.parentTaskId) : null;
        if (parent) links.append(taskLink(parent, '상위 업무'));
        for (const child of tasks.filter(row => row.parentTaskId === work.id).slice(0, 6)) links.append(taskLink(child, '하위 업무'));
        if (links.childElementCount) inspector.append(links);
      }
      const fields = element('dl', 'wd-fields');
      const position = movement.snapshot().find(row => row.id === selected);
      const entries = actor ? [['객체 상태', stateFor(actor)], ['표시 위치', roomTitle(position?.displayRoom || actor.room)],
        ['수집기', actor.sourceId], ['객체 식별자', actor.id], ['동시 진행 일감', String(actor.activeCount)]]
        : [['수집기', work.sourceId], ['객체 식별자', work.actorId]];
      if (work) entries.push([actor?.taskId === work.id ? '현재 일감' : '기록 일감', readableTaskTitle(work)],
        ['기록 출처', evidenceTitle(work.provenance)], ['수집 상태', stateFor(work)], ['처리 영역', roomTitle(work.sourceRoom)],
        ['기록 시각', localTime(work.time)], ['일감 식별자', work.hostExecutionId || work.jobId || work.id]);
      if (work?.skill) entries.push(['스킬', skillDescription(work)], ['스킬 출처', evidenceTitle(work.skill.provenance)]);
      if (work?.parentTaskId) entries.push(['상위 일감 식별자', work.parentTaskId]);
      const telemetry = (selectedTask ? work?.telemetry : actor?.telemetry) || work?.telemetry || {};
      if (actor?.kind === 'agent' || work?.kind === 'agent' || Object.keys(telemetry).length) {
        entries.splice(actor ? 1 : 0, 0, ['모델', metadataValue(telemetry.model)], ['추론 수준', metadataValue(telemetry.reasoningEffort)],
          ['토큰', usageValue(telemetry.usage)]);
      }
      for (const [key, value] of entries) {
        const entry = element('dd', '', value); fields.append(element('dt', '', key), entry);
        if (key === '표시 위치') displayRoomValue = entry;
      }
      selectedMotionNote = element('p', 'wd-detail-motion');
      const technical = element('details', 'wd-detail-technical');
      technical.append(element('summary', '', '기술 세부 정보'), fields, selectedMotionNote);
      inspector.append(technical);
      restoreInspector(); paint();
    }
    function selectThinking(id) {
      talkChoice = ''; selectedThinking = id; selected = ''; selectedTask = ''; roomFilter = '';
      for (const token of tokens.values()) token.button.classList.remove('selected');
      syncScene(); renderWorkContext(); detailSignature = ''; showDetail(); motionState();
      thinkingTokens.get(id)?.button.focus({preventScroll: true}); centerOnThinking(true); revealDetail();
    }
    function select(id) {
      talkChoice = ''; selected = id; selectedTask = ''; selectedThinking = ''; roomFilter = '';
      for (const [key, token] of tokens) token.button.classList.toggle('selected', key === id);
      syncScene(); showDetail();
      showModel();
      renderWorkContext(); motionState();
      centerOnRecord(true); revealDetail();
    }

    function selectWork(id) {
      const work = tasks.find(row => row.id === id);
      if (!work) return;
      talkChoice = ''; selected = work.actorId; selectedTask = id; selectedThinking = ''; roomFilter = '';
      for (const [key, token] of tokens) token.button.classList.toggle('selected', key === selected);
      syncScene(); showDetail(); showModel(); renderWorkContext(); motionState(); centerOnRecord(true); revealDetail();
    }

    function showModel() {
      const sourceTitles = {'chatgpt': 'ChatGPT', 'codex-cli': 'Codex CLI', 'claude-cli': 'Claude CLI'};
      const chosen = actors.find(actor => actor.id === selected && actor.kind === 'agent')
        || actors.filter(actor => actor.kind === 'agent' && Object.keys(actor.telemetry || {}).length)
          .sort((a, b) => Number(!active.has(a.state)) - Number(!active.has(b.state))
            || Date.parse(b.time) - Date.parse(a.time))[0];
      if (!chosen) { pipelineValues[0].textContent = '모델·추론 상태 미수신'; return; }
      const telemetry = chosen.telemetry || {};
      if (!telemetry.model && !telemetry.reasoningEffort) {
        pipelineValues[0].textContent = `${sourceTitles[chosen.sourceKind] || chosen.sourceKind} · 모델·추론 상태 미수신`;
        return;
      }
      const parts = [sourceTitles[chosen.sourceKind] || chosen.sourceKind,
        telemetry.model ? `${telemetry.model.value}${telemetry.model.source === 'configuration' ? ' · 설정값' : ''} · ${evidenceTitle(telemetry.model.provenance)}` : '모델 미수신',
        telemetry.reasoningEffort ? `${telemetry.reasoningEffort.value}${telemetry.reasoningEffort.source === 'configuration' ? ' · 설정값' : ''} · ${evidenceTitle(telemetry.reasoningEffort.provenance)}` : '추론 수준 미수신'];
      pipelineValues[0].textContent = parts.join(' · ');
    }

    function showChanges() {
      const available = new Set(tasks.map(task => task.id));
      const signature = JSON.stringify(recentChanges.map(item => [item, available.has(item.id)]));
      if (signature === changeSignature) return;
      changeSignature = signature; changesList.replaceChildren();
      if (!recentChanges.length) {
        changesList.append(element('span', 'wd-changes-empty', '새 작업의 시작·반환·상태 변화를 기다리는 중'));
        return;
      }
      for (const item of recentChanges) {
        const work = tasks.find(task => task.id === item.id) || item;
        const button = element('button', 'wd-change');
        button.dataset.state = item.state; button.dataset.taskId = item.id;
        button.disabled = !available.has(item.id);
        button.title = `기록 시각 ${localTime(item.time)} · 화면 변화 확인 ${localTime(item.noticedAt)}`;
        button.append(element('span', 'wd-change-action', taskGlyphs[visualAction(work)] || '[]'),
          element('span', 'wd-change-label', `${titleFor(item)} · ${stateFor(item)}`),
          element('small', '', `${evidenceTitle(item.provenance)} · 확인 ${new Date(item.noticedAt).toLocaleTimeString()}`));
        button.addEventListener('click', () => selectWork(item.id)); changesList.append(button);
      }
    }

    function signalRoom(roomId, item) {
      const room = roomNodes.get(roomId); if (!room) return;
      room.node.dataset.signal = item.state;
      if (!canMove() || typeof room.node.animate !== 'function') return;
      const color = ['failed', 'timed_out', 'canceled', 'interrupted', 'unknown'].includes(item.state)
        ? '#e99a78' : item.state === 'returned' || item.state === 'succeeded' ? '#b9d794' : '#f1ce80';
      const effect = room.node.animate([{boxShadow: `inset 0 0 0 3px ${color}, inset 0 0 26px ${color}88`},
        {boxShadow: 'inset 0 0 0 0 transparent, inset 0 0 0 transparent'}], {duration: 1400, easing: 'ease-out'});
      effects.add(effect); effect.finished.then(() => effects.delete(effect), () => effects.delete(effect));
    }

    function clearSpeeches() {
      clearTimeout(speechTimer); speechTimer = 0;
      for (const row of speeches.values()) row.node.remove();
      speeches.clear();
      for (const token of [...tokens.values(), ...thinkingTokens.values()]) {
        if (token.button.dataset.speaking === 'true') token.button.dataset.speaking = 'false';
      }
    }

    function offerSpeeches(events) {
      for (const work of events) {
        const sprite = sceneSprites.find(row => row.taskId === work.id && row.ownerId === work.actorId);
        if (!sprite || !visibleSceneIds.has(sprite.id)) continue;
        speeches.get(sprite.id)?.node.remove();
        const node = element('span', 'wd-speech'); node.hidden = true;
        node.dataset.speakerId = sprite.id; node.dataset.taskId = work.id;
        node.dataset.tone = work.speech.tone; node.dataset.event = work.speech.kind;
        const speechProvenance = work.speech.kind.startsWith('skill-')
          ? work.skill?.provenance || work.provenance : work.provenance;
        node.dataset.provenance = speechProvenance;
        node.setAttribute('role', 'status'); node.setAttribute('aria-atomic', 'true');
        const text = `${speechProvenance === 'reported' ? '보고 · ' : ''}${work.speech.text}`;
        const label = readableTaskTitle(work);
        node.append(element('strong', 'wd-speech-message', text), element('small', 'wd-speech-label', label));
        node.title = `${text} · ${label} · ${evidenceTitle(speechProvenance)}`;
        speechLayer.append(node);
        speeches.set(sprite.id, {node, work, createdAt: Date.now(), shownAt: null, skillEffectShown: false});
      }
    }

    function paintSpeeches(positions) {
      if (destroyed || stale || !following || resumeSnapshot || document.hidden) { clearSpeeches(); return; }
      clearTimeout(speechTimer); speechTimer = 0;
      const now = Date.now(), byId = new Map(positions.map(row => [row.id, row]));
      const shown = new Set(), rectangles = [], roomCounts = new Map();
      let nextExpiry = Infinity;
      const ordered = [...speeches].sort(([, a], [, b]) => b.work.speech.priority - a.work.speech.priority
        || b.createdAt - a.createdAt || a.work.id.localeCompare(b.work.id));
      for (const [id, row] of ordered) {
        const position = byId.get(id);
        const expires = row.shownAt === null ? row.createdAt + 10000 : row.shownAt + row.work.speech.durationMs;
        const current = sceneSprites.find(sprite => sprite.id === id);
        if (now >= expires || !position || position.departing || current?.taskId !== row.work.id) {
          row.node.remove(); speeches.delete(id); continue;
        }
        nextExpiry = Math.min(nextExpiry, expires);
        if (position.entryPending || position.replaying || shown.size >= 3) {
          if (!row.node.hidden) row.node.hidden = true;
          continue;
        }
        const room = position.displayRoom || 'corridor';
        const bounds = ROOMS.find(candidate => candidate.id === room);
        const minLeft = bounds ? bounds.x + 8 : 8;
        const maxLeft = bounds ? bounds.x + bounds.width - 156 : WORLD.width - 156;
        const left = Math.max(minLeft, Math.min(maxLeft, position.x - 74));
        const top = Math.max(6, position.y - 108);
        const rect = {left, right: left + 148, top, bottom: top + 54};
        if ((roomCounts.get(room) || 0) >= 2 || rectangles.some(other =>
          rect.left < other.right + 4 && rect.right + 4 > other.left
          && rect.top < other.bottom + 4 && rect.bottom + 4 > other.top)) {
          if (!row.node.hidden) row.node.hidden = true;
          continue;
        }
        if (row.shownAt === null) {
          row.shownAt = now;
          if (!row.skillEffectShown && row.work.speech.kind === 'skill-start' && canMove()) {
            row.skillEffectShown = true;
            const token = tokens.get(id) || thinkingTokens.get(id), mark = token?.button.querySelector('.wd-skill-mark');
            if (mark && typeof mark.animate === 'function') {
              const effect = mark.animate([
                {opacity: 0, transform: 'translateX(-50%) scale(.45) rotate(-25deg)'},
                {opacity: 1, transform: 'translateX(-50%) scale(1.35) rotate(8deg)', offset: .42},
                {opacity: 1, transform: 'translateX(-50%) scale(1) rotate(0)'},
              ], {duration: 1050, easing: 'ease-out'});
              effects.add(effect); effect.finished.then(() => effects.delete(effect), () => effects.delete(effect));
            }
          }
        }
        nextExpiry = Math.min(nextExpiry, row.shownAt + row.work.speech.durationMs);
        row.node.style.left = left + 'px'; row.node.style.top = top + 'px';
        row.node.style.setProperty('--wd-speech-pointer', Math.max(8, Math.min(128, position.x - left - 6)) + 'px');
        if (row.node.hidden) row.node.hidden = false;
        shown.add(id); rectangles.push(rect); roomCounts.set(room, (roomCounts.get(room) || 0) + 1);
      }
      for (const [id, token] of [...tokens, ...thinkingTokens]) {
        const speaking = String(shown.has(id));
        if (token.button.dataset.speaking !== speaking) token.button.dataset.speaking = speaking;
      }
      if (Number.isFinite(nextExpiry)) speechTimer = setTimeout(() => {
        speechTimer = 0;
        if (!destroyed) paint();
      }, Math.max(1, nextExpiry - now));
    }

    function removeSceneToken(collection, id, token) {
      speeches.get(id)?.node.remove(); speeches.delete(id);
      if (id === selectedThinking) {
        const work = tasks.find(row => row.id === token.sprite.taskId);
        selectedTask = work?.id || '';
        selected = work?.actorId || '';
        selectedThinking = ''; detailSignature = '';
        const selectionProject = currentProject;
        queueMicrotask(() => {
          if (destroyed || currentProject !== selectionProject) return;
          syncScene(); renderWorkContext(); showDetail(); motionState();
        });
      }
      token.button.remove(); collection.delete(id);
    }

    function syncScene() {
      const visible = [], visibleTasks = new Set(), autoReplay = [];
      const orderedCards = [];
      replayable.clear();
      visibleSceneIds = new Set();
      const candidates = [];
      const ordered = [...sceneSprites].sort((left, right) => Date.parse(left.invokedAt) - Date.parse(right.invokedAt)
        || (observationOrder.get(left.invocationId) ?? Number.MAX_SAFE_INTEGER)
          - (observationOrder.get(right.invocationId) ?? Number.MAX_SAFE_INTEGER));
      for (const sprite of ordered) {
        const observedCall = invocationRanks.get(sprite.invocationId);
        if (!observedCall) continue;
        const {at} = observedCall;
        const previous = invocations.get(sprite.id);
        const sameCall = previous?.id === sprite.invocationId;
        const olderCall = previous && !sameCall && (at < previous.at
          || (at === previous.at && observedCall.sequence <= previous.sequence));
        const invocation = sameCall ? {...previous, at: Math.min(previous.at, at)} : olderCall ? previous
          : observedCall;
        invocations.set(sprite.id, invocation);
        const cutoff = evicted.get(sprite.id);
        if (cutoff !== undefined && invocation.sequence > cutoff) evicted.delete(sprite.id);
        if (!evicted.has(sprite.id)) candidates.push({...sprite,
          invocationId: invocation.id, invokedAt: new Date(invocation.at).toISOString()});
      }
      const callOrder = (left, right) => invocations.get(left.id).at - invocations.get(right.id).at
        || invocations.get(left.id).sequence - invocations.get(right.id).sequence;
      for (const [id, {node, count, more, occupancy}] of roomNodes) {
        const requested = sceneSprites.filter(row => row.room === id);
        const members = candidates.filter(row => row.room === id).sort((a, b) => callOrder(b, a));
        const shown = members.slice(0, 6);
        // Every call already observed at eviction stays outside, including concurrent calls
        // that were not the selected sprite invocation when capacity was reached.
        for (const sprite of members.slice(6)) evicted.set(sprite.id, callSequence);
        const work = tasks.filter(task => taskDisplayRoom(task) === id);
        const represented = new Set(shown.map(row => row.taskId));
        const archived = window.WorkDashboardModel.recentTasks(work.filter(task => !represented.has(task.id)), {order: historyRanks});
        const archiveLimit = id === 'attention' ? Math.max(0, 6 - shown.length) : 6;
        node.dataset.activeCount = String(requested.filter(row => active.has(row.state)).length);
        count.textContent = String(work.length);
        count.title = `담당 객체 ${new Set(requested.map(row => row.ownerId)).size} · 표시 인물 ${shown.length}/6 · 일감 ${work.length}`;
        occupancy.textContent = `인물 배정 ${shown.length}/6`;
        const hiddenWork = (id === 'done' || id === 'attention') ? Math.max(0, archived.length - archiveLimit) : Math.max(0, work.length - 6);
        const hiddenPeople = requested.length - shown.length;
        more.hidden = hiddenWork === 0 && hiddenPeople === 0;
        more.textContent = [hiddenWork ? `+${hiddenWork}개 일감` : '',
          hiddenPeople ? `${hiddenPeople}명 화면 밖` : ''].filter(Boolean).join(' · ');
        if (id === 'done' || id === 'attention') {
          const room = ROOMS.find(row => row.id === id);
          for (const [index, task] of archived.slice(0, archiveLimit).entries()) {
            visibleTasks.add(task.id);
            let button = taskTokens.get(task.id);
            if (!button) {
              button = element('button', 'wd-task'); button.dataset.taskId = task.id;
              button.append(element('span', 'wd-task-icon'), element('span', 'wd-task-label'));
              button.addEventListener('click', () => selectWork(task.id)); stage.append(button); taskTokens.set(task.id, button);
            }
            const position = window.WorkDashboardMotion.slot(id, index + (id === 'attention' ? shown.length : 0));
            button.dataset.room = id; button.dataset.state = task.state; button.dataset.action = visualAction(task);
            button.dataset.historySlot = String(index);
            button.style.left = position.x - 32 + 'px'; button.style.top = position.y - 34 + 'px';
            button.style.zIndex = String(position.y);
            button.firstChild.textContent = taskGlyphs[visualAction(task)] || '[]'; button.lastChild.textContent = titleFor(task);
            button.dataset.provenance = task.provenance || 'observed';
            button.setAttribute('aria-label', `${titleFor(task)} · ${stateFor(task)} · ${evidenceTitle(task.provenance)}`);
            button.title = `${titleFor(task)} · ${stateFor(task)} · ${evidenceTitle(task.provenance)}`;
            orderedCards.push(button);
          }
        }
        for (const sprite of shown) {
          visible.push(sprite); visibleSceneIds.add(sprite.id);
          if (sprite.displayType === 'role') { ensureThinkingToken(sprite); continue; }
          for (const task of tasks.filter(row => row.actorId === sprite.id && row.replayable && sprite.state === 'idle')) {
            replayable.add(task.id);
            if (observed && canMove() && !seen.has(task.id) && Date.parse(task.time) >= seenTime) autoReplay.push(task);
          }
          let token = tokens.get(sprite.id);
          if (!token) {
            const button = element('button', 'wd-actor'), avatar = element('canvas', 'wd-avatar');
            avatar.setAttribute('aria-hidden', 'true'); button.dataset.actorId = sprite.id;
            const caption = element('span', 'wd-actor-task');
            const sparks = element('span', 'wd-work-effects'); sparks.setAttribute('aria-hidden', 'true');
            for (let index = 0; index < 3; index++) sparks.append(element('i', ''));
            const job = window.WorkDashboardModel.characterJob(sprite);
            const mark = element('span', 'wd-job-mark', job.label); mark.setAttribute('aria-hidden', 'true');
            const actionMark = element('span', 'wd-action-mark'); actionMark.setAttribute('aria-hidden', 'true');
            const skillMark = element('span', 'wd-skill-mark', '✦'); skillMark.setAttribute('aria-hidden', 'true');
            button.dataset.job = job.id;
            button.append(avatar, sparks, mark, actionMark, skillMark, element('span', 'wd-actor-label'), caption);
            button.addEventListener('click', () => select(sprite.id));
            stage.append(button); token = {button, avatar, caption, poseKey: ''}; tokens.set(sprite.id, token);
          }
          token.work = tasks.find(row => row.id === sprite.taskId) || token.work;
          token.button.dataset.room = sprite.room; token.button.dataset.state = sprite.state; token.button.dataset.kind = sprite.kind;
          token.button.dataset.provenance = sprite.provenance || 'reported';
          token.button.dataset.observationStatus = sprite.observationStatus?.kind || '';
          let observationBadge = token.button.querySelector('.wd-actor-observation');
          if (observationTitle(sprite)) {
            if (!observationBadge) {
              observationBadge = element('span', 'wd-actor-observation', '종료 미확인');
              token.button.append(observationBadge);
            }
            observationBadge.title = `${observationTitle(sprite)} · 시작 기록 ${localTime(sprite.observationStatus.time)} · 종료 기록은 최근 수신 범위에서 확인되지 않음`;
          } else observationBadge?.remove();
          token.button.classList.toggle('selected', sprite.id === selected);
          token.button.querySelector('.wd-actor-label').textContent = sprite.label;
          const observationLabel = observationTitle(sprite) ? ` · ${observationTitle(sprite)}` : '';
          token.button.setAttribute('aria-label', `${kindFor(sprite)} · ${titleFor(sprite)} · ${states[sprite.state]} · ${roomTitle(sprite.room)}${observationLabel}`);
          token.button.title = `${titleFor(sprite)} · ${states[sprite.state]} · ${evidenceTitle(sprite.provenance)} · ${localTime(sprite.time)}${observationLabel}`;
        }
      }
      const focusedCard = document.activeElement;
      let nextCard = null;
      for (const card of [...orderedCards].reverse()) {
        if (card.nextElementSibling !== nextCard) stage.insertBefore(card, nextCard);
        nextCard = card;
      }
      if (orderedCards.includes(focusedCard) && document.activeElement !== focusedCard) focusedCard.focus({preventScroll: true});
      const leaving = movement.snapshot().filter(row => !visibleSceneIds.has(row.id) && !row.departing);
      movement.depart(leaving.map(row => row.id), {animate: canMove()});
      movement.sync(visible.sort(callOrder), {initial: !observed, animate: !reducedMotion.matches,
        replayTasks: autoReplay, staggerEntries: true});
      const retained = new Set(movement.snapshot().map(row => row.id));
      for (const collection of [tokens, thinkingTokens]) for (const [id, token] of collection) {
        if (!retained.has(id)) removeSceneToken(collection, id, token);
      }
      for (const [id, button] of taskTokens) if (!visibleTasks.has(id)) { button.remove(); taskTokens.delete(id); }
      renderThinkingRoles();
    }

    function paint() {
      const age = lastQueryAt ? Math.max(0, Math.floor((Date.now() - lastQueryAt) / 1000)) : 0;
      const phase = stale ? 'stale' : !following ? 'paused' : document.hidden ? 'hidden' : resumeSnapshot ? 'resuming' : !lastQueryAt ? 'loading' : age > 10 ? 'delayed' : 'live';
      liveStatus.dataset.state = phase;
      liveStatus.textContent = phase === 'live' ? `화면 갱신 · ${age < 2 ? '방금' : age + '초 전'}`
        : {stale: '관측 지연', paused: '관측 일시정지', hidden: '화면 비활성', resuming: '최신 상태 동기화 중', loading: '기록 확인 중', delayed: `마지막 갱신 ${age}초 전`}[phase];
      const positions = movement.snapshot();
      let replays = 0, moving = 0, departures = 0;
      for (const item of positions) {
        const role = thinkingTokens.get(item.id);
        const token = role || tokens.get(item.id); if (!token) continue;
        const {actor, walking, replaying, direction, replayTask} = item;
        token.button.hidden = Boolean(item.entryPending);
        token.button.dataset.entryPending = String(Boolean(item.entryPending));
        token.button.style.transform = `translate(${(item.x - 32).toFixed(2)}px, ${(item.y - 44).toFixed(2)}px)`;
        token.button.style.zIndex = String(Math.round(item.y));
        token.button.dataset.walking = String(walking);
        token.button.dataset.presentation = item.departing ? 'departing' : replaying ? 'replay' : 'observed';
        token.button.dataset.displayRoom = item.displayRoom || 'corridor';
        const work = replayTask || tasks.find(row => row.id === actor.taskId) || token.work;
        const working = !item.departing && !walking && ((active.has(work?.state || actor.state) && item.displayRoom === actor.room)
          || (replaying && item.displayRoom === replayTask?.sourceRoom));
        const action = work ? visualAction(work) : actor.action;
        token.button.dataset.action = working ? action : 'idle'; token.button.dataset.working = String(working);
        const skill = work?.skill;
        if (skill) {
          token.button.dataset.skillId = skill.id;
          token.button.dataset.skillPhase = work.state;
          token.button.dataset.skillProvenance = skill.provenance;
        } else {
          delete token.button.dataset.skillId;
          delete token.button.dataset.skillPhase;
          delete token.button.dataset.skillProvenance;
        }
        const skillActive = Boolean(skill && working && active.has(work.state) && !replaying && canMove());
        token.button.dataset.skillActive = String(skillActive);
        const skillMark = token.button.querySelector('.wd-skill-mark');
        if (skillMark) skillMark.title = skill ? `스킬 · ${skillDescription(work)} · ${stateFor(work)} · ${
          evidenceTitle(skill.provenance)}` : '';
        const actionMark = token.button.querySelector('.wd-action-mark');
        if (actionMark) {
          actionMark.textContent = working ? taskGlyphs[action] || '>_' : '';
          actionMark.title = working ? actionTitles[action] || '작업' : '';
        }
        const provenance = work?.provenance || actor.provenance;
        token.button.dataset.provenance = provenance || 'reported';
        if (role) {
          role.work = work;
          role.button.querySelector('.wd-thinking-state').textContent = item.departing
            ? `${evicted.has(item.id) ? '정원 교체' : stateFor(work)} · 퇴장` : roleStatus(actor, work);
        } else {
          const actionText = work?.skill ? `${evidenceTitle(work.skill.provenance)} · 스킬 ${skillLabel(work)}`
            : actor.state === 'idle' ? '대기' : work ? readableAction(work)
            : actor.state === 'unobserved' ? '활동 미수신' : '대기';
          const taskText = work ? `${readableTaskTitle(work)}${actor.activeCount > 1 ? ` +${actor.activeCount - 1}` : ''}` : states[actor.state];
          const exitText = evicted.has(item.id) ? '정원 교체' : stateFor(work);
          const caption = item.departing ? `${exitText} · 퇴장\n${taskText}` : replaying ? `기록 재생 · ${actionText}\n${taskText}`
            : `${actionText} · ${work ? stateFor(work) : states[actor.state]}\n${taskText}`;
          token.caption.textContent = work?.skill ? caption : provenance === 'reported' ? `보고 · ${caption}` : caption;
          token.caption.title = work ? `${readableAction(work)} · ${readableTaskTitle(work)}` : states[actor.state];
          if (work?.skill && !item.departing) {
            const accessible = `${actorName(actor)} · 스킬 ${skillDescription(work)} · ${
              evidenceTitle(work.skill.provenance)} · ${stateFor(work)} · ${readableTaskTitle(work)} · ${roomTitle(item.displayRoom)}${
              observationTitle(actor) ? ' · ' + observationTitle(actor) : ''}`;
            if (token.button.getAttribute('aria-label') !== accessible) token.button.setAttribute('aria-label', accessible);
          }
          if (item.departing) {
            token.button.dataset.state = work?.state || actor.state;
            token.button.setAttribute('aria-label', `${actorName(actor)} · ${exitText} · 퇴장 · ${roomTitle(item.displayRoom)}`);
            token.button.title = `${readableTaskTitle(work)} · ${exitText} · 퇴장`;
          }
        }
        const gesturing = working && (!role || provenance === 'observed');
        const animationFrame = canMove() ? Math.floor(clock * (walking ? 7 : gesturing ? 4 : 1)) : 0;
        const poseKey = [direction, walking, gesturing, action, actor.state, actor.room, animationFrame].join(':');
        if (poseKey !== token.poseKey) {
          window.WorkDashboardPixels.drawPerson(token.avatar, {...actor, action, direction, walking, working: gesturing, replaying, clock});
          token.poseKey = poseKey;
        }
        if (replaying) replays++; if (walking) moving++; if (item.departing) departures++;
        if (item.id === selected) {
          if (displayRoomValue) displayRoomValue.textContent = roomTitle(item.displayRoom);
          if (selectedMotionNote) selectedMotionNote.textContent = replaying ?
            `${evidenceTitle(work?.provenance)} 기록 재생 · 현재 상태는 위의 수집 상태를 따릅니다.` : '';
        }
      }
      const retained = new Set(positions.map(row => row.id));
      for (const collection of [tokens, thinkingTokens]) for (const [id, token] of collection) {
        if (retained.has(id) || visibleSceneIds.has(id)) continue;
        removeSceneToken(collection, id, token);
      }
      paintSpeeches(positions);
      movingScene = replays > 0 || moving > 0 || departures > 0;
      const text = stale ? '관측 지연 · 마지막 확인 기록' : !following ? '관측 일시정지' :
        replays ? `관측 기록 재생${motion ? '' : ' 일시정지'} · ${replays}명` :
          !motion ? '움직임 일시정지 · 관측 상태 표시' : activeCount ? `진행 중 ${activeCount}건 관측` :
            reducedMotion.matches ? '동작 감소 · 관측 상태 표시' :
              departures ? `결과 기록 후 퇴장 · ${departures}명` : moving ? '새 상태의 이동을 표시 중' : '새로운 작업 이벤트를 기다리는 중';
      if (observation.textContent !== text) observation.textContent = text;
      observation.dataset.replaying = String(replays > 0);
      replayButton.disabled = !replayable.size || !canMove();
    }
    function animate(timestamp) {
      frame = 0;
      if (destroyed || !canMove() || !hasMotionActors()) { lastFrame = 0; return; }
      const delta = lastFrame ? Math.min((timestamp - lastFrame) / 1000, 0.2) : 0;
      lastFrame = timestamp; clock += delta; movement.advance(delta); paint();
      if (movingScene) frame = requestAnimationFrame(animate);
      else idleTimer = setTimeout(() => {
        idleTimer = 0;
        if (!destroyed && canMove()) frame = requestAnimationFrame(animate);
      }, 90);
    }
    function motionState() {
      root.classList.toggle('wd-motion-paused', !canMove());
      motionButton.textContent = motion ? '움직임 일시정지' : '움직임 재개';
      motionButton.setAttribute('aria-pressed', String(!motion));
      if (canMove() && hasMotionActors() && !frame && !idleTimer) frame = requestAnimationFrame(animate);
      else if (!canMove()) {
        cancelAnimationFrame(frame); clearTimeout(idleTimer); frame = 0; idleTimer = 0; lastFrame = 0;
        for (const effect of effects) effect.cancel(); effects.clear();
      }
      paint();
    }
    motionButton.addEventListener('click', () => { motion = !motion; motionState(); });
    replayButton.addEventListener('click', () => {
      if (!canMove()) return;
      movement.replay([...replayable], tasks); motionState();
    });
    const mediaChanged = () => { if (reducedMotion.matches) movement.settle(); motionState(); };
    reducedMotion.addEventListener('change', mediaChanged);
    function suspendObservation() {
      if (resumeSnapshot || !observed || !previousSnapshot) return;
      resumeSnapshot = previousSnapshot; suspendedAt = Date.now(); suspendedObservedAt = lastObservedAt;
      root.dataset.resuming = 'true'; resumeNotice.hidden = false;
      resumeNotice.textContent = '복귀 시 최신 상태를 확인합니다. 서버의 실제 작업은 계속됩니다.';
    }
    function canResume(input) {
      if (!resumeSnapshot || input.error || input.following === false || document.hidden) return false;
      const next = Date.parse(input.hostObservedAt), previous = Date.parse(suspendedObservedAt);
      // The parent first resends cached data, then posts the successful host poll.
      if (Number.isFinite(next)) return Number.isFinite(previous) ? next > previous : next >= suspendedAt;
      return !Object.hasOwn(input, 'hostObservedAt');
    }
    function summarizeReturn(data) {
      const delta = window.WorkDashboardModel.resumeChanges(resumeSnapshot, data, {since: suspendedAt});
      const succeeded = delta.completed.filter(task => task.state === 'succeeded').length;
      const returned = delta.completed.filter(task => task.state === 'returned').length;
      const parts = [];
      if (delta.newTasks.length) parts.push(`새로 확인한 일감 ${delta.newTasks.length}`);
      if (succeeded) parts.push(`작업 성공 ${succeeded}`);
      if (returned) parts.push(`요청 반환 ${returned}`);
      if (delta.attention.length) parts.push(`확인 필요 ${delta.attention.length}`);
      if (delta.missing.length) parts.push(`이전 기록 ${delta.missing.length}개는 조회 범위 밖`);
      resumeNotice.textContent = `복귀 동기화 완료 · ${localTime(new Date().toISOString())} · ${parts.join(' · ') || '새로 확인한 상태 변화 없음'}`;
      const changed = [...new Map([...delta.newTasks, ...delta.changed].map(task => [task.id, task])).values()];
      recentChanges = window.WorkDashboardModel.recentTasks(changed).slice(0, 3)
        .map(task => ({...task, noticedAt: new Date().toISOString()}));
    }
    const visibilityChanged = () => { if (document.hidden) suspendObservation(); motionState(); };
    document.addEventListener('visibilitychange', visibilityChanged);

    function update(input) {
      if (currentProject !== input.project) clear(input.project);
      if ((following && input.following === false) || document.hidden) suspendObservation();
      if (resumeSnapshot && (input.following === false || document.hidden)) {
        // An in-flight poll can finish while paused; its cached return message is not a fresh poll.
        const next = Date.parse(input.hostObservedAt), previous = Date.parse(suspendedObservedAt);
        if (Number.isFinite(next) && (!Number.isFinite(previous) || next > previous)) {
          suspendedObservedAt = input.hostObservedAt;
        }
      }
      const data = window.WorkDashboardModel.snapshot({...input, objects: [...registry.values()]});
      const resumed = canResume(input);
      const speechEvents = speechReady && !input.error && input.following !== false && !resumeSnapshot && !document.hidden
        ? window.WorkDashboardModel.speechEvents(previousSnapshot, data) : [];
      if (resumed) summarizeReturn(data);
      actors = data.actors; tasks = data.tasks;
      observationOrder = new Map();
      const remember = id => { if (!observationOrder.has(id)) observationOrder.set(id, observationOrder.size); };
      // Activity lists are newest-first; preserve first observation order for simultaneous calls.
      const oldestFirst = rows => (Array.isArray(rows) ? [...rows].reverse() : []);
      for (const row of oldestFirst(input.events)) if (typeof row?.call_id === 'string') remember(`call:${row.call_id}`);
      for (const row of oldestFirst(input.records)) {
        if (![row?.source_id, row?.actor_id, row?.task_id].every(value => typeof value === 'string')) continue;
        remember(`task:${encodeURIComponent(row.source_id)}:${encodeURIComponent(row.actor_id)}:${encodeURIComponent(row.task_id)}`);
      }
      for (const row of oldestFirst(input.jobs)) if (typeof row?.job_id === 'string') remember(`job:${row.job_id}`);
      const hostTasks = new Map(tasks.filter(task => task.hostExecutionId).map(task => [task.hostExecutionId, task.id]));
      for (const row of oldestFirst(input.hostExecutions)) {
        const id = hostTasks.get(row?.operation_id);
        if (id) remember(id);
      }
      const history = tasks.filter(task => task.room === 'done' || task.room === 'attention')
        .sort((left, right) => Date.parse(left.time) - Date.parse(right.time)
          || (observationOrder.get(left.id) ?? Number.MAX_SAFE_INTEGER)
            - (observationOrder.get(right.id) ?? Number.MAX_SAFE_INTEGER));
      for (const task of history) if (!historyRanks.has(task.id)) historyRanks.set(task.id, ++historySequence);
      const called = tasks.filter(task => Number.isFinite(Date.parse(task.invokedAt)))
        .sort((left, right) => Date.parse(left.invokedAt) - Date.parse(right.invokedAt)
          || (observationOrder.get(left.id) ?? Number.MAX_SAFE_INTEGER)
            - (observationOrder.get(right.id) ?? Number.MAX_SAFE_INTEGER));
      for (const task of called) {
        const previous = invocationRanks.get(task.id), at = Date.parse(task.invokedAt);
        invocationRanks.set(task.id, previous ? {...previous, at: Math.min(previous.at, at)}
          : {id: task.id, at, sequence: ++callSequence});
      }
      const invocationOrder = new Map(called.map(task => [task.id, invocationRanks.get(task.id).sequence]));
      const currentSprites = window.WorkDashboardModel.scene(data, {invocationOrder}).sprites;
      const currentOwners = new Map(actors.map(actor => [actor.id, actor]));
      // Retained idle figures must not retain lifecycle evidence that left the query window.
      for (const [id, sprite] of idleSprites) idleSprites.set(id, {...sprite,
        observationStatus: currentOwners.get(sprite.ownerId)?.observationStatus});
      const currentIds = new Set(currentSprites.map(sprite => sprite.id));
      for (const sprite of currentSprites) {
        if (sprite.state === 'idle') idleSprites.set(sprite.id, sprite);
        else idleSprites.delete(sprite.id);
      }
      sceneSprites = currentSprites.concat([...idleSprites.values()].filter(sprite => !currentIds.has(sprite.id)));
      project.textContent = data.project || '';
      for (const actor of actors) registry.set(actor.id, actor);
      activeCount = data.counts.active;
      following = input.following !== false; stale = Boolean(input.error); root.dataset.stale = String(stale);
      if (!stale) {
        lastQueryAt = Date.now();
        if (following && !resumeSnapshot) for (const item of window.WorkDashboardModel.changes(previousSnapshot, data)) {
          recentChanges.unshift(item); recentChanges = recentChanges.slice(0, 3);
          signalRoom(item.sourceRoom, item);
          if (item.room !== item.sourceRoom) signalRoom(item.room, item);
        }
        previousSnapshot = data;
        lastObservedAt = input.hostObservedAt ?? null;
        if (!Object.hasOwn(input, 'hostObservedAt') || Number.isFinite(Date.parse(input.hostObservedAt))) speechReady = true;
      }
      const claudeChildren = actors.filter(actor => actor.sourceKind === 'claude-cli' && actor.localId.startsWith('sub:'));
      const activeChildren = claudeChildren.filter(actor => active.has(actor.state) && actor.provenance === 'observed').length;
      const childStatus = claudeChildren.length ? ` · 하위 에이전트 활동 관측 ${activeChildren}명` : '';
      status.textContent = input.error ? `조회 실패 · 마지막 확인 기록 유지 · ${input.error}` :
        `${following ? '관찰 중' : '관찰 일시정지'} · 진행 ${data.counts.active}건 · 미확인 ${data.counts.unknown} · 실패·중단 기록 ${data.counts.attention}${childStatus}`;
      status.title = `현재 조회 범위: 객체 ${data.entityCount} · ${data.counts.total}개 일감 · 반환/성공 ${data.counts.done}. 실패·중단 기록은 미해결 장애 수가 아닙니다.`;
      pipelineValues[2].textContent = actors.length ? [...new Set(actors.map(actor => actor.sourceKind))].join(' · ') + ' · 최근 ' + localTime(data.latestEventAt) : '수집 객체 없음';
      showModel(); showChanges(); renderWorkContext(); renderThinkingRoles();
      pipelineValues[3].textContent = `진행 ${data.counts.active} · 기록 ${data.counts.total}`;
      syncScene();
      offerSpeeches(speechEvents);
      if (resumed) {
        movement.settle(); resumeSnapshot = null; suspendedAt = 0; suspendedObservedAt = null;
        root.dataset.resuming = 'false';
        renderWorkContext(); renderThinkingRoles();
      }
      if (!stale) {
        observed = true; for (const task of tasks) seen.add(task.id);
        seenTime = Math.max(seenTime, Date.parse(data.latestEventAt) || -Infinity);
        if (seen.size > 512) {
          const recent = [...seen].slice(-256); seen.clear();
          for (const id of recent) seen.add(id);
        }
      }
      centerOnRecord();
      motionState(); showDetail();
    }
    function clear(nextProject = '') {
      clearSpeeches();
      talkChoice = ''; missionTaskId = '';
      speechReady = false;
      currentProject = nextProject; actors = []; tasks = []; sceneSprites = []; visibleSceneIds.clear(); selected = ''; selectedTask = ''; selectedThinking = ''; roomFilter = ''; stale = false;
      activeCount = 0; movingScene = false;
      cameraTarget = '';
      observed = false; seenTime = -Infinity; seen.clear(); replayable.clear(); movement.clear();
      previousSnapshot = null; recentChanges = []; lastQueryAt = 0; detailSignature = ''; changeSignature = '';
      resumeSnapshot = null; suspendedAt = 0; lastObservedAt = null; suspendedObservedAt = null;
      resumeNotice.hidden = true; resumeNotice.textContent = ''; root.dataset.resuming = 'false';
      for (const effect of effects) effect.cancel(); effects.clear();
      registry.clear(); invocations.clear(); invocationRanks.clear(); evicted.clear(); idleSprites.clear(); observationOrder.clear(); callSequence = 0;
      historyRanks.clear(); historySequence = 0;
      cancelAnimationFrame(frame); clearTimeout(idleTimer); frame = 0; idleTimer = 0; lastFrame = 0; clock = 0;
      for (const token of tokens.values()) token.button.remove(); tokens.clear();
      for (const button of taskTokens.values()) button.remove(); taskTokens.clear();
      for (const card of brainCards.values()) card.remove(); brainCards.clear();
      for (const item of workItems.values()) item.remove(); workItems.clear();
      for (const token of thinkingTokens.values()) token.button.remove(); thinkingTokens.clear();
      for (const {node, count, more, occupancy} of roomNodes.values()) {
        count.textContent = '0'; occupancy.textContent = '인물 배정 0/6'; more.hidden = true; node.dataset.activeCount = '0'; delete node.dataset.signal;
      }
      project.textContent = nextProject; status.textContent = nextProject ? '기록 불러오는 중…' : '연결 전';
      pipelineValues[0].textContent = '모델·추론 상태 미수신';
      pipelineValues[2].textContent = '호출 기록 없음'; pipelineValues[3].textContent = '실행 기록 없음';
      root.dataset.stale = 'false'; showChanges(); renderWorkContext(); renderThinkingRoles(); motionState(); showDetail();
    }
    function setLive(value) {
      if (!value && following) suspendObservation();
      following = value;
      if (!stale) status.textContent = status.textContent.replace(/^관찰 (?:중|일시정지)/,
        following ? '관찰 중' : '관찰 일시정지');
      renderWorkContext(); renderThinkingRoles(); detailSignature = ''; showDetail(); motionState();
    }
    function destroy() {
      clearSpeeches();
      speechReady = false;
      destroyed = true; observer.disconnect(); cancelAnimationFrame(frame); clearTimeout(idleTimer); movement.clear(); tokens.clear(); taskTokens.clear(); registry.clear();
      invocations.clear(); invocationRanks.clear(); evicted.clear(); idleSprites.clear(); observationOrder.clear();
      historyRanks.clear(); resumeSnapshot = null;
      brainCards.clear(); workItems.clear(); thinkingTokens.clear();
      for (const effect of effects) effect.cancel(); effects.clear();
      reducedMotion.removeEventListener('change', mediaChanged); document.removeEventListener('visibilitychange', visibilityChanged);
      root.replaceChildren();
    }
    clear();
    return {update, clear, setLive, destroy};
  }
  window.WorkDashboard = {create};
})();
