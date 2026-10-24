'use strict';
/* Owner-authenticated one-use approval for exact, registered host commands.
 * Never constructs argv, executes a command or displays secret/raw output.
 */
(() => {
  const $ = id => document.getElementById(id);
  if (!$('host-command-approval')) return;
  const ID = /^[0-9a-f]{32}$/, SHA = /^[0-9a-f]{64}$/;
  let plans = [], selected = '', selectedProject = '';

  const status = message => { $('host-command-status').textContent = String(message || ''); };
  const eligible = p => Boolean(
    p && p.state === 'pending_host_approval' && ID.test(p.operation_id) &&
    SHA.test(p.plan_digest) && SHA.test(p.source_digest) &&
    Date.parse(p.expires_at) > Date.now()
  );
  function selectedPlan() { return plans.find(p => p.operation_id === selected) || null; }

  function render() {
    const list = $('host-command-plan');
    list.replaceChildren();
    for (const plan of plans) {
      if (!ID.test(plan.operation_id)) continue;
      const choice = document.createElement('option');
      choice.value = plan.operation_id;
      choice.textContent = plan.operation_id + ' · ' + plan.profile_id + ' · ' + plan.effect;
      list.append(choice);
    }
    if (!plans.some(p => p.operation_id === selected)) selected = plans.length ? plans[0].operation_id : '';
    list.value = selected;
    const plan = selectedPlan();
    const safe = plan ? {
      project: plan.project, operation_id: plan.operation_id, profile_id: plan.profile_id,
      label: plan.label, effect: plan.effect, state: plan.state, phase: plan.phase,
      source_digest: plan.source_digest, plan_digest: plan.plan_digest,
      created_at: plan.created_at, expires_at: plan.expires_at,
    } : null;
    $('host-command-details').textContent = safe
      ? JSON.stringify(safe, null, 2)
      : '현재 이 프로젝트에 승인 대기 중인 호스트 명령 계획이 없습니다.';
    $('host-command-approve').disabled = !eligible(plan);
    $('host-command-guidance').textContent = plan
      ? '이 승인은 명령을 실행하지 않습니다. ChatGPT에서 별도 execute_host_command 요청이 필요합니다.'
      : 'ChatGPT에서 호스트 소유 프로필을 조회하고 prepare_host_command를 호출하세요.';
  }

  async function request(url, options = {}) {
    const response = await fetch(url, {
      ...options, credentials: 'same-origin', cache: 'no-store',
      headers: {'Content-Type': 'application/json'},
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(typeof data.code === 'string' && /^[A-Z][A-Z0-9_]{2,63}$/.test(data.code)
        ? data.code : '요청이 거부됐습니다 (' + response.status + ')');
    }
    return data;
  }

  async function refresh() {
    const project = $('project').value;
    selectedProject = project;
    plans = []; selected = '';
    if (!project) { render(); return; }
    try {
      const result = await request('/api/host-command-plans?project=' + encodeURIComponent(project));
      if (project !== $('project').value) return;
      plans = Array.isArray(result.plans) ? result.plans : [];
      status('');
    } catch (error) {
      plans = [];
      status(String(error.message || '승인 계획을 읽지 못했습니다.'));
    }
    const requested = new URLSearchParams(location.search).get('operation_id');
    if (requested && ID.test(requested) && plans.some(p => p.operation_id === requested)) {
      selected = requested;
    }
    render();
  }

  $('host-command-plan').addEventListener('change', () => {
    selected = $('host-command-plan').value;
    $('host-command-confirmation').value = '';
    $('host-command-token').value = '';
    render();
  });
  $('host-command-refresh').addEventListener('click', refresh);
  $('project').addEventListener('change', refresh);
  $('host-command-form').addEventListener('submit', async event => {
    event.preventDefault();
    const plan = selectedPlan();
    if (!eligible(plan) || !selectedProject || selectedProject !== $('project').value
        || $('host-command-confirmation').value !== plan.operation_id
        || !$('host-command-token').value) {
      status('정확한 계획 ID와 새 관리 토큰을 입력해야 합니다.');
      return;
    }
    $('host-command-approve').disabled = true;
    try {
      const result = await request('/api/host-command-approve?project=' +
        encodeURIComponent(selectedProject), {
        method: 'POST', body: JSON.stringify({
          operation_id: plan.operation_id,
          plan_digest: plan.plan_digest,
          source_digest: plan.source_digest,
          confirmation: $('host-command-confirmation').value,
          admin_token: $('host-command-token').value,
        }),
      });
      status(result.approved === true
        ? '승인이 저장됐습니다. ChatGPT에서 명시적인 실행 요청을 진행하세요.'
        : '승인 확인에 실패했습니다.');
    } catch (error) {
      status(String(error.message || '승인 실패'));
    } finally {
      $('host-command-token').value = '';
      $('host-command-confirmation').value = '';
      await refresh();
    }
  });
  // Project choices arrive after the main admin script authenticates.
  new MutationObserver(() => {
    if (!$('dashboard').hidden && $('project').value !== selectedProject) {
      void refresh();
    }
  }).observe($('project'), {childList: true});
  render();
})();
