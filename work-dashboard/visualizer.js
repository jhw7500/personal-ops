'use strict';
(() => {
  const roomInfo = {
    waiting: ['대기실', '객체 대기 · 대기 일감'], analysis: ['분석실', '파일 조회 · 탐색 · 추론 신호'],
    coding: ['코딩실', '파일 생성 · 수정'], review: ['검토실', 'Git · 작업 보고'],
    execution: ['실행실', '명령 실행'], testing: ['검사실', '빌드 · 테스트'],
    done: ['기록 보관실', '반환 · 성공 일감 보관'], attention: ['확인 필요', '실패 · 중단 · 취소 일감']
  };
  const states = {started: '시작 기록', returned: '요청 반환', queued: '대기', running: '실행 중',
    canceling: '취소 중', succeeded: '명령 성공', failed: '실패', timed_out: '시간 초과',
    canceled: '취소됨', interrupted: '중단됨', unknown: '종료 상태 미확인', idle: '대기 중', unobserved: '최근 범위 기록 없음'};
  const toolTitles = {read_file: '파일 읽기', list_dir: '목록 조회', find_file: '파일 찾기',
    search_for_pattern: '내용 검색', create_text_file: '파일 생성', replace_content: '코드 수정',
    start_command: '실행 요청', git_status: 'Git 상태', git_diff: 'Git diff',
    read_project_memory: '메모 읽기', update_project_memory: '메모 저장',
    read_project_progress: '보고 읽기', update_project_progress: '보고 저장',
    list_execution_profiles: '명령 조회', prepare_git_operation: 'Git 계획',
    execute_git_operation: 'Git 실행', get_git_operation: 'Git 결과'};
  const active = new Set(['started', 'running', 'canceling']);
  const sessionTitles = {'Codex session active': '세션 활동 중', 'Codex session waiting': '세션 대기',
    'Codex session inactive': '활동 확인 대기'};
  const titleFor = actor => toolTitles[actor.tool] || (actor.kind === 'job' && actor.label === '직접 명령 실행'
    ? actionTitles[actor.action] || actor.label : sessionTitles[actor.label] || actor.label);
  const kindFor = item => ({call: item.sourceKind === 'mcp' ? 'MCP 호출' : '도구 호출', job: '명령 실행', agent: '에이전트 작업',
    mcp: 'MCP 서버', executor: '명령 실행기'}[item.kind] || '객체');
  const stateFor = item => item.kind === 'agent' && item.state === 'succeeded' ? '작업 완료' : states[item.state];
  const actionTitles = {read: '책 읽기', search: '자료 탐색', reason: '추론 신호', code: '코드 작성', build: '빌드 조립',
    test: '검사', review: '변경 비교', execute: '명령 실행', commit: '커밋 기록', push: '전송', idle: '대기'};
  const taskGlyphs = {read: '[]', search: '?', reason: '*', code: '</>', build: '#', test: 'OK', review: '<>',
    execute: '>_', commit: 'G', push: '>>'};
  const roomTitle = id => roomInfo[id]?.[0] || '공용 복도';
  const localTime = time => time ? new Date(time).toLocaleString() : '기록 없음';
  const evidenceTitle = value => value === 'observed' ? '직접 관측' : '보고됨';
  const usageScopes = {request: '요청 단위', turn: '턴 단위', session: '세션 단위', task: '작업 단위'};
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

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    if (tag === 'button') node.type = 'button';
    return node;
  }

  function create(root, {onJobSelect = () => {}} = {}) {
    const {WORLD, ROOMS} = window.WorkDashboardMotion;
    const movement = window.WorkDashboardMotion.create();
    root.className = 'panel wd-map'; root.dataset.stale = 'false';
    let actors = [], tasks = [], selected = '', selectedTask = '', currentProject = '', roomFilter = '';
    let motion = true, following = true, stale = false, observed = false, destroyed = false;
    let frame = 0, idleTimer = 0, lastFrame = 0, clock = 0, zoom = 1, zoomMode = 'auto', seenTime = -Infinity;
    let activeCount = 0, movingScene = false;
    let previousSnapshot = null, lastQueryAt = 0, detailSignature = '', changeSignature = '';
    let recentChanges = [];
    const effects = new Set();
    let cameraTarget = '';
    let displayRoomValue = null, selectedMotionNote = null;
    const seen = new Set(), replayable = new Set(), tokens = new Map(), taskTokens = new Map(), roomNodes = new Map(), registry = new Map();
    const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');

    const heading = element('div', 'section-heading');
    const title = element('div');
    title.append(element('p', 'eyebrow', 'WORK GUILD'), element('h2', '', '업무 작업실'));
    const project = element('span', 'badge'); project.id = 'wd-project';
    const liveStatus = element('span', 'wd-live-status'); liveStatus.id = 'wd-live-status';
    liveStatus.title = '화면의 상태 조회 시각입니다. 각 작업의 관측 시각은 기록에서 확인할 수 있습니다.';
    const motionButton = element('button', '', '움직임 일시정지'); motionButton.id = 'wd-motion';
    heading.append(title, project, liveStatus, motionButton);
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
    const toolbar = element('div', 'wd-toolbar');
    const observation = element('span', 'wd-observation'); observation.id = 'wd-observation';
    const replayButton = element('button', '', '최근 활동 재생'); replayButton.id = 'wd-replay';
    replayButton.title = '확인한 시작·종료 기록의 이동을 다시 봅니다. 명령을 다시 실행하지 않습니다.';
    const zoomOut = element('button', 'wd-zoom', '−'); zoomOut.id = 'wd-zoom-out';
    const zoomIn = element('button', 'wd-zoom', '+'); zoomIn.id = 'wd-zoom-in';
    zoomOut.setAttribute('aria-label', '지도 축소'); zoomIn.setAttribute('aria-label', '지도 확대');
    const fitButton = element('button', '', '전체 보기'); fitButton.id = 'wd-fit';
    const zoomValue = element('span', 'wd-zoom-value'); zoomValue.id = 'wd-zoom-value';
    toolbar.append(observation, replayButton, zoomOut, zoomValue, zoomIn, fitButton);

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
      count.addEventListener('click', () => { roomFilter = room.id; selected = ''; selectedTask = ''; showDetail(); });
      top.append(element('h3', '', name), lamp, count);
      const more = element('button', 'wd-more'); more.hidden = true;
      more.addEventListener('click', () => { roomFilter = room.id; selected = ''; selectedTask = ''; showDetail(); });
      node.append(top, element('p', 'wd-room-description', description), more);
      floor.append(node); roomNodes.set(room.id, {node, count, more});
    }
    stage.append(mapCanvas, floor); sizer.append(stage); camera.append(sizer);
    const inspector = element('aside', 'wd-inspector'); inspector.id = 'wd-detail';
    layout.append(camera, inspector);
    const hint = element('p', 'muted wd-hint', '지도를 끌거나 스크롤해 둘러보세요. 인물과 방의 개수를 누르면 기록을 확인할 수 있습니다.');
    const legend = element('p', 'muted wd-legend', '청록 가방 · MCP 서버   황금 가방 · 명령 실행기   보라 가방 · 에이전트   문서 · 완료 일감');
    const note = element('p', 'muted wd-note', '인물은 고정된 작업 객체이며 완료 일감은 보관실에 남습니다. 직접 관측과 채팅 보고를 구분하며, 재생은 과거 기록입니다. 모델·추론 수준·토큰은 제공된 값만 표시합니다. 추론 동작에는 명시적인 시작·종료 신호가 필요하며 ChatGPT 내부 추론은 자동 수집되지 않습니다.');
    root.replaceChildren(heading, status, pipeline, changesPanel, toolbar, layout, hint, legend, note);

    const canMove = () => motion && following && !stale && !reducedMotion.matches && !document.hidden;
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
    function centerOnRecord(force = false) {
      if (!actors.length || (!force && (zoomMode !== 'auto' || window.innerWidth > 760))) return;
      const actor = actors.find(row => row.id === selected) || tasks.find(row => row.id === selectedTask)
        || actors.find(row => active.has(row.state)) || actors[0];
      const room = ROOMS.find(row => row.id === actor.room);
      const target = actor.room + ':' + selected;
      if (!force && cameraTarget === target) return;
      cameraTarget = target;
      const position = selected ? movement.snapshot().find(row => row.id === selected) : null;
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

    function showDetail() {
      const signature = JSON.stringify([selected, selectedTask, roomFilter, actors, tasks]);
      if (signature === detailSignature) return;
      detailSignature = signature;
      inspector.replaceChildren(); displayRoomValue = null; selectedMotionNote = null;
      const actor = actors.find(row => row.id === selected);
      const work = tasks.find(row => row.id === (selectedTask || actor?.taskId || actor?.lastTaskId));
      if (!actor && !work) {
        inspector.append(element('p', 'eyebrow', 'WORK INSPECTOR'), element('h3', '', roomFilter ? roomTitle(roomFilter) : '객체와 일감'));
        const list = roomFilter ? tasks.filter(row => row.room === roomFilter) : [];
        inspector.append(element('p', 'muted', list.length ? '확인할 일감을 선택하세요.' : '객체나 보관된 일감을 선택하면 작업과 원본 출력을 확인할 수 있습니다.'));
        for (const row of actors.filter(row => !roomFilter || row.room === roomFilter)) {
          const button = element('button', 'wd-detail-item wd-entity-item', `${row.label} · ${states[row.state]}`);
          button.addEventListener('click', () => select(row.id)); inspector.append(button);
        }
        for (const row of list) {
          const button = element('button', 'wd-detail-item', `${kindFor(row)} · ${titleFor(row)} · ${stateFor(row)}`);
          button.dataset.taskId = row.id;
          button.addEventListener('click', () => selectWork(row.id)); inspector.append(button);
        }
        return;
      }
      inspector.append(element('p', 'eyebrow', kindFor(actor || work)), element('h3', '', actor ? actor.label : titleFor(work)));
      const fields = element('dl', 'wd-fields');
      const position = movement.snapshot().find(row => row.id === selected);
      const entries = actor ? [['객체 상태', stateFor(actor)], ['표시 위치', roomTitle(position?.displayRoom || actor.room)],
        ['수집기', actor.sourceId], ['객체 식별자', actor.id], ['동시 진행 일감', String(actor.activeCount)]]
        : [['수집기', work.sourceId], ['객체 식별자', work.actorId]];
      if (work) entries.push([actor?.taskId === work.id ? '현재 일감' : '기록 일감', titleFor(work)],
        ['기록 출처', evidenceTitle(work.provenance)], ['수집 상태', stateFor(work)], ['처리 영역', roomTitle(work.sourceRoom)],
        ['기록 시각', localTime(work.time)], ['일감 식별자', work.jobId || work.id]);
      if (work?.parentTaskId) entries.push(['상위 일감', work.parentTaskId]);
      const telemetry = (selectedTask ? work?.telemetry : actor?.telemetry) || work?.telemetry || {};
      if (actor?.kind === 'agent' || work?.kind === 'agent' || Object.keys(telemetry).length) {
        entries.splice(actor ? 1 : 0, 0, ['모델', metadataValue(telemetry.model)], ['추론 수준', metadataValue(telemetry.reasoningEffort)],
          ['토큰', usageValue(telemetry.usage)]);
      }
      for (const [key, value] of entries) {
        const entry = element('dd', '', value); fields.append(element('dt', '', key), entry);
        if (key === '표시 위치') displayRoomValue = entry;
      }
      selectedMotionNote = element('p', 'wd-detail-motion'); inspector.append(fields, selectedMotionNote);
      if (work?.jobId) {
        const open = element('button', '', '원본 출력 열기'); open.id = 'wd-open-log';
        open.addEventListener('click', () => onJobSelect(work.jobId)); inspector.append(open);
      } else {
        inspector.append(element('p', 'muted', work?.state === 'returned' ?
          '요청 반환 기록입니다. 명령 성공 여부는 명령의 종료 상태와 원본 출력에서 확인하세요.' :
          '수집된 객체와 일감의 상태를 표시합니다.'));
      }
      const related = tasks.filter(row => row.actorId === (actor?.id || work.actorId) && row.id !== work?.id).slice(0, 8);
      if (related.length) inspector.append(element('p', 'muted', '이 객체의 다른 일감'));
      for (const row of related) {
        const button = element('button', 'wd-detail-item', `${titleFor(row)} · ${stateFor(row)}`);
        button.dataset.taskId = row.id; button.addEventListener('click', () => selectWork(row.id)); inspector.append(button);
      }
      paint();
    }
    function select(id) {
      selected = id; selectedTask = ''; roomFilter = '';
      for (const [key, token] of tokens) token.button.classList.toggle('selected', key === id);
      showDetail();
      showModel();
      centerOnRecord(true);
    }

    function selectWork(id) {
      const work = tasks.find(row => row.id === id);
      if (!work) return;
      selected = work.actorId; selectedTask = id; roomFilter = '';
      for (const [key, token] of tokens) token.button.classList.toggle('selected', key === selected);
      showDetail(); showModel(); centerOnRecord(true);
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
        const button = element('button', 'wd-change');
        button.dataset.state = item.state; button.dataset.taskId = item.id;
        button.disabled = !available.has(item.id);
        button.title = `기록 시각 ${localTime(item.time)} · 화면 변화 확인 ${localTime(item.noticedAt)}`;
        button.append(element('span', 'wd-change-action', taskGlyphs[item.action] || '[]'),
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

    function paint() {
      const age = lastQueryAt ? Math.max(0, Math.floor((Date.now() - lastQueryAt) / 1000)) : 0;
      const phase = stale ? 'stale' : !following ? 'paused' : document.hidden ? 'hidden' : !lastQueryAt ? 'loading' : age > 10 ? 'delayed' : 'live';
      liveStatus.dataset.state = phase;
      liveStatus.textContent = phase === 'live' ? `화면 갱신 · ${age < 2 ? '방금' : age + '초 전'}`
        : {stale: '관측 지연', paused: '관측 일시정지', hidden: '화면 비활성', loading: '기록 확인 중', delayed: `마지막 갱신 ${age}초 전`}[phase];
      const positions = movement.snapshot();
      let replays = 0, moving = 0;
      for (const item of positions) {
        const token = tokens.get(item.id); if (!token) continue;
        const {actor, walking, replaying, direction, replayTask} = item;
        token.button.style.transform = `translate(${(item.x - 32).toFixed(2)}px, ${(item.y - 44).toFixed(2)}px)`;
        token.button.style.zIndex = String(Math.round(item.y));
        token.button.dataset.walking = String(walking); token.button.dataset.presentation = replaying ? 'replay' : 'observed';
        token.button.dataset.displayRoom = item.displayRoom || 'corridor';
        const working = !walking && ((active.has(actor.state) && item.displayRoom === actor.room)
          || (replaying && item.displayRoom === replayTask?.sourceRoom));
        const action = replayTask?.action || actor.action;
        const work = replayTask || tasks.find(row => row.id === actor.taskId);
        token.button.dataset.action = working ? action : 'idle'; token.button.dataset.working = String(working);
        const caption = replaying ? `재생 · ${work ? titleFor(work) : ''}`
          : work ? `${titleFor(work)}${actor.activeCount > 1 ? ` +${actor.activeCount - 1}` : ''}`
            : actor.state === 'unobserved' ? '최근 기록 없음' : '대기';
        const provenance = work?.provenance || actor.provenance;
        token.button.dataset.provenance = provenance || 'reported';
        token.caption.textContent = provenance === 'reported' ? `보고 · ${caption}` : caption;
        token.caption.title = work ? `${actionTitles[action]} · ${titleFor(work)}` : states[actor.state];
        const animationFrame = canMove() ? Math.floor(clock * (walking ? 7 : working ? 4 : 1)) : 0;
        const poseKey = [direction, walking, working, action, actor.state, actor.room, animationFrame].join(':');
        if (poseKey !== token.poseKey) {
          window.WorkDashboardPixels.drawPerson(token.avatar, {...actor, action, direction, walking, working, replaying, clock});
          token.poseKey = poseKey;
        }
        if (replaying) replays++; if (walking) moving++;
        if (item.id === selected) {
          if (displayRoomValue) displayRoomValue.textContent = roomTitle(item.displayRoom);
          if (selectedMotionNote) selectedMotionNote.textContent = replaying ?
            `${evidenceTitle(work?.provenance)} 기록 재생 · 현재 상태는 위의 수집 상태를 따릅니다.` : '';
        }
      }
      movingScene = replays > 0 || moving > 0;
      const text = stale ? '관측 지연 · 마지막 확인 기록' : !following ? '관측 일시정지' :
        replays ? `관측 기록 재생${motion ? '' : ' 일시정지'} · ${replays}명` :
          !motion ? '움직임 일시정지 · 관측 상태 표시' : activeCount ? `진행 중 ${activeCount}건 관측` :
            reducedMotion.matches ? '동작 감소 · 관측 상태 표시' :
              moving ? '새 상태의 이동을 표시 중' : '새로운 작업 이벤트를 기다리는 중';
      if (observation.textContent !== text) observation.textContent = text;
      observation.dataset.replaying = String(replays > 0);
      replayButton.disabled = !replayable.size || !canMove();
    }
    function animate(timestamp) {
      frame = 0;
      if (destroyed || !canMove() || !tokens.size) { lastFrame = 0; return; }
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
      if (canMove() && tokens.size && !frame && !idleTimer) frame = requestAnimationFrame(animate);
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
    const visibilityChanged = () => { if (!document.hidden) previousSnapshot = null; motionState(); };
    document.addEventListener('visibilitychange', visibilityChanged);

    function update(input) {
      if (currentProject !== input.project) clear(input.project);
      const data = window.WorkDashboardModel.snapshot({...input, objects: [...registry.values()]});
      actors = data.actors; tasks = data.tasks; project.textContent = data.project || '';
      for (const actor of actors) registry.set(actor.id, actor);
      activeCount = data.counts.active;
      following = input.following !== false; stale = Boolean(input.error); root.dataset.stale = String(stale);
      if (!stale) {
        lastQueryAt = Date.now();
        if (following) for (const item of window.WorkDashboardModel.changes(previousSnapshot, data)) {
          recentChanges.unshift(item); recentChanges = recentChanges.slice(0, 3);
          signalRoom(item.sourceRoom, item);
          if (item.room !== item.sourceRoom) signalRoom(item.room, item);
        }
        previousSnapshot = data;
      }
      status.textContent = input.error ? `조회 실패 · 마지막 확인 기록 유지 · ${input.error}` :
        `${following ? '자동 관찰' : '자동 관찰 일시정지'} · 객체 ${data.entityCount} · 진행 ${data.counts.active} · 반환/성공 ${data.counts.done} · 확인 필요 ${data.counts.attention} · 미확인 ${data.counts.unknown} · ${data.counts.total}개 일감`;
      pipelineValues[2].textContent = actors.length ? [...new Set(actors.map(actor => actor.sourceKind))].join(' · ') + ' · 최근 ' + localTime(data.latestEventAt) : '수집 객체 없음';
      showModel(); showChanges();
      pipelineValues[3].textContent = `진행 ${data.counts.active} · 기록 ${data.counts.total}`;
      const visibleActors = [], visible = new Set(), visibleTasks = new Set(), autoReplay = [];
      replayable.clear();
      for (const [id, {node, count, more}] of roomNodes) {
        const members = actors.filter(actor => actor.room === id);
        const work = tasks.filter(task => task.room === id);
        node.dataset.activeCount = String(members.filter(actor => active.has(actor.state)).length);
        count.textContent = String(work.length); count.title = `객체 ${members.length} · 일감 ${work.length}`;
        more.hidden = work.length <= 6 && members.length <= 6;
        more.textContent = [work.length > 6 ? `+${work.length - 6}개 일감` : '',
          members.length > 6 ? `+${members.length - 6}개 객체` : ''].filter(Boolean).join(' · ');
        if (id === 'done' || id === 'attention') {
          const room = ROOMS.find(row => row.id === id);
          for (const [index, task] of work.slice(0, 6).entries()) {
            visibleTasks.add(task.id);
            let button = taskTokens.get(task.id);
            if (!button) {
              button = element('button', 'wd-task'); button.dataset.taskId = task.id;
              button.append(element('span', 'wd-task-icon'), element('span', 'wd-task-label'));
              button.addEventListener('click', () => selectWork(task.id)); stage.append(button); taskTokens.set(task.id, button);
            }
            button.dataset.room = id; button.dataset.state = task.state; button.dataset.action = task.action;
            button.style.left = room.x + 16 + (index % 3) * 68 + 'px';
            button.style.top = room.y + 94 + Math.floor(index / 3) * 62 + 'px';
            button.style.zIndex = String(room.y + 120 + Math.floor(index / 3) * 62);
            button.firstChild.textContent = taskGlyphs[task.action] || '[]'; button.lastChild.textContent = titleFor(task);
            button.dataset.provenance = task.provenance || 'observed';
            button.setAttribute('aria-label', `${titleFor(task)} · ${stateFor(task)} · ${evidenceTitle(task.provenance)}`);
            button.title = `${titleFor(task)} · ${stateFor(task)} · ${evidenceTitle(task.provenance)}`;
          }
        }
        for (const actor of members.slice(0, 6)) {
          visibleActors.push(actor); visible.add(actor.id);
          for (const task of tasks.filter(row => row.actorId === actor.id && row.replayable && actor.state === 'idle')) {
            replayable.add(task.id);
            if (observed && canMove() && !seen.has(task.id) && Date.parse(task.time) >= seenTime) autoReplay.push(task);
          }
          let token = tokens.get(actor.id);
          if (!token) {
            const button = element('button', 'wd-actor'), avatar = element('canvas', 'wd-avatar');
            avatar.setAttribute('aria-hidden', 'true'); button.dataset.actorId = actor.id;
            const caption = element('span', 'wd-actor-task');
            const sparks = element('span', 'wd-work-effects'); sparks.setAttribute('aria-hidden', 'true');
            for (let index = 0; index < 3; index++) sparks.append(element('i', ''));
            button.append(avatar, sparks, element('span', 'wd-actor-label'), caption);
            button.addEventListener('click', () => select(actor.id));
            stage.append(button); token = {button, avatar, caption, poseKey: ''}; tokens.set(actor.id, token);
          }
          token.button.dataset.room = actor.room; token.button.dataset.state = actor.state; token.button.dataset.kind = actor.kind;
          token.button.dataset.provenance = actor.provenance || 'reported';
          token.button.classList.toggle('selected', actor.id === selected);
          token.button.querySelector('.wd-actor-label').textContent = actor.label;
          token.button.setAttribute('aria-label', `${kindFor(actor)} · ${titleFor(actor)} · ${states[actor.state]} · ${roomTitle(actor.room)}`);
          token.button.title = `${titleFor(actor)} · ${states[actor.state]} · ${evidenceTitle(actor.provenance)} · ${localTime(actor.time)}`;
        }
      }
      for (const [id, token] of tokens) if (!visible.has(id)) { token.button.remove(); tokens.delete(id); }
      for (const [id, button] of taskTokens) if (!visibleTasks.has(id)) { button.remove(); taskTokens.delete(id); }
      const initialObservation = !observed;
      movement.sync(visibleActors, {initial: initialObservation, animate: !reducedMotion.matches, replayTasks: autoReplay});
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
      currentProject = nextProject; actors = []; tasks = []; selected = ''; selectedTask = ''; roomFilter = ''; stale = false;
      activeCount = 0; movingScene = false;
      cameraTarget = '';
      observed = false; seenTime = -Infinity; seen.clear(); replayable.clear(); movement.clear();
      previousSnapshot = null; recentChanges = []; lastQueryAt = 0; detailSignature = ''; changeSignature = '';
      for (const effect of effects) effect.cancel(); effects.clear();
      registry.clear();
      cancelAnimationFrame(frame); clearTimeout(idleTimer); frame = 0; idleTimer = 0; lastFrame = 0; clock = 0;
      for (const token of tokens.values()) token.button.remove(); tokens.clear();
      for (const button of taskTokens.values()) button.remove(); taskTokens.clear();
      for (const {node, count, more} of roomNodes.values()) {
        count.textContent = '0'; more.hidden = true; node.dataset.activeCount = '0'; delete node.dataset.signal;
      }
      project.textContent = nextProject; status.textContent = nextProject ? '기록 불러오는 중…' : '연결 전';
      pipelineValues[0].textContent = '모델·추론 상태 미수신';
      pipelineValues[2].textContent = '호출 기록 없음'; pipelineValues[3].textContent = '실행 기록 없음';
      root.dataset.stale = 'false'; showChanges(); motionState(); showDetail();
    }
    function setLive(value) { if (value && !following) previousSnapshot = null; following = value; motionState(); }
    function destroy() {
      destroyed = true; observer.disconnect(); cancelAnimationFrame(frame); clearTimeout(idleTimer); movement.clear(); tokens.clear(); taskTokens.clear(); registry.clear();
      for (const effect of effects) effect.cancel(); effects.clear();
      reducedMotion.removeEventListener('change', mediaChanged); document.removeEventListener('visibilitychange', visibilityChanged);
      root.replaceChildren();
    }
    clear();
    return {update, clear, setLive, destroy};
  }
  window.WorkDashboard = {create};
})();
