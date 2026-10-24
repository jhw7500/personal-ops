'use strict';
(() => {
  const VERSION = 1;
  const HEIGHT_MIN = 480;
  const HEIGHT_MAX = 1600;
  const exact = (value, keys) => value && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
  let epoch = 0;
  let project = '';
  let nonce = '';
  const post = message => parent.postMessage(message, '*');
  const dashboard = window.WorkDashboard.create(document.getElementById('work-map'), {
    onJobSelect: jobId => post({type: 'personal-ops:workroom:select-job', version: VERSION,
      epoch, project, nonce, jobId}),
    onHostExecutionSelect: operationId => post({
      type: 'personal-ops:workroom:select-host-execution', version: VERSION,
      epoch, project, nonce, operationId,
    }),
  });
  function summary(snapshot) {
    const normalized = window.WorkDashboardModel.snapshot({...snapshot, now: Date.now()});
    const executions = normalized.tasks.filter(task => task.kind === 'job' || task.kind === 'agent');
    return {
      active: executions.filter(task => ['started', 'queued', 'running', 'canceling'].includes(task.state)).length,
      failed: executions.filter(task => ['failed', 'timed_out'].includes(task.state)).length,
      attention: normalized.counts.attention,
      unknown: normalized.counts.unknown,
      total: normalized.counts.total,
    };
  }
  window.addEventListener('message', event => {
    if (event.source !== parent || event.origin !== location.origin
        || !exact(event.data, ['type', 'version', 'epoch', 'nonce', 'snapshot'])) return;
    const message = event.data;
    if (message.type !== 'personal-ops:workroom:update' || message.version !== VERSION
        || !Number.isSafeInteger(message.epoch) || message.epoch < epoch
        || typeof message.nonce !== 'string'
        || !/^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/.test(message.nonce)
        || !exact(message.snapshot, ['project', 'jobs', 'events', 'records', 'hostExecutions',
          'hostObservedAt', 'following', 'error'])) return;
    epoch = message.epoch;
    project = message.snapshot.project;
    nonce = message.nonce;
    dashboard.update(message.snapshot);
    dashboard.setLive(message.snapshot.following === true);
    post({type: 'personal-ops:workroom:summary', version: VERSION, epoch, project, nonce,
      counts: summary(message.snapshot)});
  });
  let pending = 0;
  let lastHeight = 0;
  const mapRoot = document.getElementById('work-map');
  const sendHeight = () => {
    pending = 0;
    const height = Math.max(HEIGHT_MIN, Math.min(HEIGHT_MAX,
      Math.ceil(mapRoot.offsetTop + mapRoot.offsetHeight + 8)));
    if (height === lastHeight) return;
    lastHeight = height;
    post({type: 'personal-ops:workroom:height', version: VERSION, epoch, project, nonce, height});
  };
  const observer = new ResizeObserver(() => {
    if (!pending) pending = requestAnimationFrame(sendHeight);
  });
  observer.observe(mapRoot);
  post({type: 'personal-ops:workroom:ready', version: VERSION});
})();
