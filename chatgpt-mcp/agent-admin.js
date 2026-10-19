'use strict';
/* Agent approval UI: authenticated web only, no direct Codex execution. */
(() => {
  const $ = id => document.getElementById(id);
  const ID = /^[0-9a-f]{32}$/;
  const SHA = /^[0-9a-f]{64}$/;
  const root = $('agent-approval');
  if (!root) return;
  let tasks = [], selected = '', project = '';
  const status = msg => { $('agent-status').textContent = String(msg || ''); };
  const selectedTask = () => tasks.find(task => task.task_id === selected) || null;
  function eligible(task) {
    return Boolean(task && task.state === 'pending_host_approval'
      && Date.parse(task.expires_at) > Date.now()
      && ID.test(task.task_id) && SHA.test(task.plan_digest)
      && SHA.test(task.source_digest));
  }
  function render() {
    const select = $('agent-plan');
    select.replaceChildren();
    for (const task of tasks) {
      if (!ID.test(task.task_id)) continue;
      const option = document.createElement('option');
      option.value = task.task_id;
      option.textContent = task.task_id + ' · ' + task.profile_id + ' · ' + task.state;
      select.append(option);
    }
    if (!tasks.some(x => x.task_id === selected)) selected = tasks.length ? tasks[0].task_id : '';
    select.value = selected;
    const task = selectedTask();
    const metadata = task ? {
      project: task.project, task_id: task.task_id, state: task.state,
      model: task.model, reasoning_effort: task.reasoning_effort,
      profile_id: task.profile_id, reason: task.reason, objective: task.objective,
      objective_sha256: task.objective_sha256, source_digest: task.source_digest,
      plan_digest: task.plan_digest, allowed_paths: task.allowed_paths,
      changed_paths: task.changed_paths, expires_at: task.expires_at,
    } : null;
    // textContent only: never interpret model/user text as DOM HTML.
    $('agent-details').textContent = metadata ? JSON.stringify(metadata, null, 2)
      : '이 프로젝트의 승인 대기 에이전트 작업이 없습니다.';
    $('agent-approve').disabled = !eligible(task);
    $('agent-availability').textContent = task
      ? '한 번 승인 후 ChatGPT에서 별도 실행 요청이 필요합니다.'
      : 'ChatGPT에서 prepare_agent_task로 계획을 먼저 만드세요.';
  }
  async function request(path, options = {}) {
    const response = await fetch(path, {
      ...options, credentials: 'same-origin', cache: 'no-store',
      headers: {'Content-Type': 'application/json'},
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(
      body && typeof body.code === 'string' && /^[A-Z][A-Z0-9_]{2,63}$/.test(body.code)
        ? body.code : '관리자 인증 또는 계획 검증 실패 (' + response.status + ')');
    return body;
  }
  async function refresh() {
    project = $('project').value;
    tasks = []; selected = '';
    if (!project) { render(); return; }
    try {
      const data = await request('/api/agent-tasks?project=' + encodeURIComponent(project));
      if (project !== $('project').value) return;
      tasks = Array.isArray(data.tasks) ? data.tasks : [];
      status('');
    } catch (error) {
      tasks = [];
      status(String(error.message || '조회 실패'));
    }
    render();
  }
  $('agent-plan').addEventListener('change', () => {
    selected = $('agent-plan').value;
    $('agent-confirmation').value = '';
    $('agent-token').value = '';
    render();
  });
  $('agent-refresh').addEventListener('click', refresh);
  $('project').addEventListener('change', refresh);
  $('agent-approval-form').addEventListener('submit', async event => {
    event.preventDefault();
    const task = selectedTask();
    if (!eligible(task) || !project || $('agent-confirmation').value !== task.task_id
        || !$('agent-token').value) {
      status('정확한 미만료 작업 ID와 새 관리 토큰이 필요합니다.');
      return;
    }
    const button = $('agent-approve');
    button.disabled = true;
    try {
      const result = await request('/api/agent-approve?project=' + encodeURIComponent(project), {
        method: 'POST',
        body: JSON.stringify({
          task_id: task.task_id, plan_digest: task.plan_digest,
          source_digest: task.source_digest,
          confirmation: $('agent-confirmation').value,
          admin_token: $('agent-token').value,
        }),
      });
      status(result.approved === true ? '승인 완료. 에이전트 실행은 ChatGPT에서 별도 요청하세요.'
        : '승인 완료가 확인되지 않았습니다.');
    } catch (error) {
      status(String(error.message || '승인 실패'));
    } finally {
      $('agent-token').value = '';
      $('agent-confirmation').value = '';
      await refresh();
    }
  });
  render();
})();
