'use strict';
/* Owner-only one-use approval of a frozen personal-ops runtime. No shell inputs. */
(() => {
  const $ = id => document.getElementById(id);
  if (!$('runtime-rollout-approval')) return;
  const HEX32 = /^[a-f0-9]{32}$/;
  const HEX64 = /^[a-f0-9]{64}$/;
  const RESULT_KEYS = new Set(['status', 'snapshot', 'previous_snapshot', 'installed',
    'switched', 'tools_registered', 'connector_schemas_verified', 'code']);
  const query = new URLSearchParams(location.search);
  const queryKeys = [...query.keys()];
  const requested = location.pathname === '/runtime-rollouts'
    && queryKeys.length === 1 && queryKeys[0] === 'operation_id'
    && HEX32.test(query.get('operation_id') || '') ? query.get('operation_id') : '';
  let plans = [], selected = '', detail = null, requestedPending = Boolean(requested);
  const status = value => { $('runtime-rollout-status').textContent = String(value || ''); };
  const eligible = v => Boolean(v && v.state === 'pending_host_approval'
    && HEX32.test(v.operation_id) && HEX64.test(v.plan_digest)
    && HEX64.test(v.source_digest) && Date.parse(v.expires_at) > Date.now());
  const current = () => detail && detail.operation_id === selected ? detail : null;

  function boundedResult(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    const result = {};
    for (const [key, item] of Object.entries(value)) {
      if (!RESULT_KEYS.has(key) || !['string', 'boolean'].includes(typeof item)) continue;
      result[key] = typeof item === 'string' ? item.slice(0, 320) : item;
    }
    return Object.keys(result).length ? result : null;
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      ...options, credentials: 'same-origin', cache: 'no-store',
      headers: {'Content-Type': 'application/json'},
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const code = typeof payload.code === 'string' && /^[A-Z_]{3,60}$/.test(payload.code)
        ? payload.code : String(response.status);
      throw new Error('요청이 거부됐습니다 (' + code + ').');
    }
    return payload;
  }

  function render() {
    const options = $('runtime-rollout-plan');
    options.replaceChildren();
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = plans.length
      ? '런타임 계획을 선택하세요.'
      : '표시할 런타임 계획이 없습니다.';
    options.append(placeholder);
    for (const plan of plans) {
      if (!HEX32.test(plan.operation_id)) continue;
      const option = document.createElement('option');
      option.value = plan.operation_id;
      option.textContent = plan.operation_id + ' · ' + plan.state + ' · ' + plan.target_snapshot;
      options.append(option);
    }
    if (!plans.some(x => x.operation_id === selected)) selected = '';
    options.value = selected;
    const plan = current();
    const expired = Boolean(plan && plan.state === 'pending_host_approval'
      && !(Date.parse(plan.expires_at) > Date.now()));
    const data = plan ? {
      project: plan.project, operation_id: plan.operation_id, state: plan.state,
      phase: plan.phase, code: plan.code,
      source_files: plan.source_files, source_bytes: plan.source_bytes,
      source_digest: plan.source_digest, target_snapshot: plan.target_snapshot,
      control_digest: plan.control_digest, plan_digest: plan.plan_digest,
      created_at: plan.created_at, expires_at: plan.expires_at,
      impact: plan.impact, started_at: plan.started_at, finished_at: plan.finished_at,
      approval: expired ? '만료 · 새 계획 필요' : plan.approval_required
        ? '승인 대기' : '추가 승인 불가',
      execution_overdue: plan.execution_overdue === true
        ? '실행 시간 초과 관측 · 상태 확인 필요' : false,
      outcome_requires_review: plan.outcome_requires_review === true
        ? '결과 검토 필요 · 자동 재실행 금지' : false,
      result: boundedResult(plan.result),
    } : null;
    $('runtime-rollout-details').textContent = data
      ? JSON.stringify(data, null, 2)
      : '현재 승인 대기 중인 MCP 런타임 업데이트 계획이 없습니다.';
    $('runtime-rollout-approve').disabled = !eligible(plan);
  }

  async function loadSelected(operationId) {
    if (!HEX32.test(operationId || '')) {
      detail = null;
      render();
      return false;
    }
    const result = await request('/api/runtime-rollout?project=personal-ops&operation_id='
      + encodeURIComponent(operationId));
    if (!result || result.project !== 'personal-ops' || result.operation_id !== operationId) {
      throw new Error('요청한 런타임 계획의 범위가 일치하지 않습니다.');
    }
    detail = result;
    if (!plans.some(plan => plan.operation_id === operationId)) plans.unshift(result);
    selected = operationId;
    render();
    return true;
  }

  async function refresh() {
    selected = $('runtime-rollout-plan').value || selected;
    try {
      const result = await request('/api/runtime-rollout-plans?project=personal-ops');
      plans = Array.isArray(result.plans) ? result.plans : [];
      if (requestedPending) {
        requestedPending = false;
        selected = '';
        detail = null;
        try {
          await loadSelected(requested);
          status('');
        } catch {
          status('링크의 런타임 계획을 찾을 수 없습니다. 목록에서 다른 계획을 선택할 수 있습니다.');
          render();
        }
        return;
      }
      if (selected && plans.some(plan => plan.operation_id === selected)) {
        await loadSelected(selected);
      } else {
        selected = '';
        detail = null;
        render();
      }
      status('');
    } catch (error) {
      plans = [];
      selected = '';
      detail = null;
      status(error.message || '계획 목록 조회 실패');
      render();
    }
  }

  $('runtime-rollout-refresh').addEventListener('click', refresh);
  $('runtime-rollout-plan').addEventListener('change', async () => {
    selected = $('runtime-rollout-plan').value;
    requestedPending = false;
    detail = null;
    $('runtime-rollout-confirmation').value = '';
    $('runtime-rollout-token').value = '';
    render();
    if (!selected) return;
    try {
      await loadSelected(selected);
      status('');
    } catch (error) {
      detail = null;
      status(error.message || '계획 조회 실패');
      render();
    }
  });
  $('runtime-rollout-form').addEventListener('submit', async event => {
    event.preventDefault();
    const plan = current();
    const confirmation = $('runtime-rollout-confirmation').value;
    const token = $('runtime-rollout-token').value;
    if (!eligible(plan) || confirmation !== plan.operation_id || !token) {
      status('정확한 작업 ID와 새 관리자 토큰이 필요합니다.');
      return;
    }
    $('runtime-rollout-approve').disabled = true;
    let approvalStatus = '';
    try {
      const result = await request('/api/runtime-rollout-approve?project=personal-ops', {
        method: 'POST',
        body: JSON.stringify({
          operation_id: plan.operation_id, plan_digest: plan.plan_digest,
          source_digest: plan.source_digest, confirmation, admin_token: token,
        }),
      });
      approvalStatus = result.approved === true
        ? '일회성 승인이 기록되었습니다. 채팅에서 별도 실행을 요청해야 실제 서비스가 전환됩니다.'
        : '승인 상태를 확인하지 못했습니다.';
    } catch (error) {
      approvalStatus = error.message || '승인 실패';
    } finally {
      $('runtime-rollout-confirmation').value = '';
      $('runtime-rollout-token').value = '';
      await refresh();
      status(approvalStatus);
    }
  });
  window.addEventListener('personal-ops:authenticated', () => {
    if (!$('dashboard').hidden && !$('view-access').hidden) void refresh();
  });
  new MutationObserver(() => {
    if (!$('dashboard').hidden && !$('view-access').hidden) void refresh();
  }).observe($('view-access'), {attributes: true, attributeFilter: ['hidden']});
  render();
})();
