const PANEL_IDS = {
  cycle: 'cycleDetails',
  sync: 'syncDetails',
  aiFix: 'aiDetails',
  settings: 'settingsDetails',
  backups: 'backupBody',
};

function valueOrDash(value) {
  if (value === undefined || value === null || value === '') return '-';
  return String(value);
}

function boolLabel(value) {
  if (value === true) return 'ON';
  if (value === false) return 'OFF';
  return '-';
}

function formatTime(value) {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value).replace('T', ' ').slice(0, 19) : date.toLocaleString();
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes)) return '-';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatDuration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return '-';
  if (value >= 3600) return `${value / 3600}h`;
  if (value >= 60) return `${Math.round(value / 60)}m`;
  return `${value}s`;
}

function replaceChildren(parent, children) {
  parent.replaceChildren(...children);
}

function makeDetail(label, value) {
  const item = document.createElement('div');
  item.className = 'detail-item';
  const title = document.createElement('dt');
  title.textContent = label;
  const content = document.createElement('dd');
  content.textContent = valueOrDash(value);
  item.append(title, content);
  return item;
}

function renderDetails(id, pairs) {
  const target = document.getElementById(id);
  if (!target) return;
  replaceChildren(target, pairs.map(([label, value]) => makeDetail(label, value)));
}

function panelRoot(name) {
  return document.querySelector(`[data-panel="${name}"]`);
}

function setPanel(name, state, message) {
  const root = panelRoot(name);
  if (!root) return;
  root.dataset.state = state;
  const health = root.querySelector('[data-panel-health]');
  const note = root.querySelector('[data-panel-message]');
  if (health) {
    health.textContent = state === 'ok' ? 'LIVE' : state === 'error' ? 'UNAVAILABLE' : 'LOADING';
    health.className = `health health-${state}`;
  }
  if (note && message) note.textContent = message;
}

function contextValue(context, ...names) {
  for (const name of names) {
    if (context[name] !== undefined && context[name] !== null && context[name] !== '') return context[name];
  }
  return 'Not exposed by current read contract';
}

function contextAuthority(context) {
  for (const name of ['control_authority', 'controlAuthority']) {
    if (context[name] !== undefined && context[name] !== null && context[name] !== '') return context[name];
  }
  return 'READ ONLY / FAIL CLOSED';
}

function renderContext(state) {
  const context = state.general?.profile_context || state.general?.context
    || state.cycle?.profile_context || {};
  const values = {
    contextHost: contextValue(context, 'host_id', 'hostId'),
    contextProfile: contextValue(context, 'profile_id', 'profileId'),
    contextAccount: contextValue(context, 'remote_account', 'remoteAccount', 'account_ref'),
    contextIdentity: contextValue(context, 'verified_identity', 'verifiedIdentity', 'identity_ref') === 'Not exposed by current read contract'
      ? 'UNVERIFIED'
      : contextValue(context, 'verified_identity', 'verifiedIdentity', 'identity_ref'),
    contextAuthority: contextAuthority(context),
  };
  Object.entries(values).forEach(([id, value]) => {
    const target = document.getElementById(id);
    if (target) target.textContent = valueOrDash(value);
  });
}

function renderCards(state) {
  const cycle = state.cycle || {};
  const simple = state.cycleSimple || {};
  const running = simple.running ?? cycle.cycle_running;
  const sync = state.sync || {};
  const watcher = (state.aiFix || {}).watcher || {};
  const general = state.general || {};
  const cycleCard = document.getElementById('cardCycle');
  const cycleMeta = document.getElementById('cardCycleMeta');
  const syncCard = document.getElementById('cardSync');
  const syncMeta = document.getElementById('cardSyncMeta');
  const aiCard = document.getElementById('cardAi');
  const aiMeta = document.getElementById('cardAiMeta');
  const clientsCard = document.getElementById('cardClients');
  const clientsMeta = document.getElementById('cardClientsMeta');
  if (cycleCard) cycleCard.textContent = running ? 'RUNNING' : 'OFFLINE';
  if (cycleMeta) cycleMeta.textContent = running ? `PID ${valueOrDash(cycle.cycle_pid)}` : 'No active cycle';
  if (syncCard) syncCard.textContent = sync.continuous_running ? 'RUNNING' : 'STOPPED';
  if (syncMeta) syncMeta.textContent = sync.continuous_running ? `Every ${formatDuration(sync.interval_sec)}` : 'No active worker';
  if (aiCard) aiCard.textContent = watcher.auto ? 'WATCHING' : `${((state.aiFix || {}).pending || []).length} PENDING`;
  if (aiMeta) aiMeta.textContent = watcher.last_action ? `Last: ${watcher.last_action}` : 'Queue read-only';
  if (clientsCard) clientsCard.textContent = valueOrDash(general.clients ?? sync.total_clients);
  if (clientsMeta) clientsMeta.textContent = general.lastUpdated ? `Updated ${general.lastUpdated}` : 'Client count';
}

function renderCycle(state) {
  const data = state.cycle;
  const simple = state.cycleSimple;
  if (!data || !simple) {
    setPanel('cycle', 'error', `Cycle read unavailable: ${state.errors.cycle || state.errors.cycleSimple || 'missing source'}`);
    renderDetails(PANEL_IDS.cycle, []);
    return;
  }
  const running = simple.running ?? data.cycle_running;
  const disabled = Array.isArray(data.alwaysrun_disabled) ? data.alwaysrun_disabled.join(', ') : '-';
  const logs = Array.isArray(data.last_log) ? data.last_log[data.last_log.length - 1] : data.last_log;
  renderDetails(PANEL_IDS.cycle, [
    ['Status', running ? 'Running' : 'Offline'],
    ['Cycle PID', data.cycle_pid],
    ['Sync PID', data.sync_pid],
    ['360Auto', data['360auto']],
    ['qnyh', data.qnyh],
    ['Scheduled', data.cycle_paused ? 'PAUSED' : 'ACTIVE'],
    ['Fixed', data.alwaysrun_paused ? 'PAUSED' : 'ACTIVE'],
    ['Fixed OFF', disabled],
    ['Manual overrides', Array.isArray(data.manual_overrides) ? data.manual_overrides.length : 0],
    ['Today', data.state && data.state.today],
    ['Last cycle log', logs],
  ]);
  setPanel('cycle', 'ok', 'Live read from the golden cycle status contract.');
}

function renderSync(state) {
  const data = state.sync;
  if (!data) {
    setPanel('sync', 'error', `Auto Sync read unavailable: ${state.errors.sync || 'missing source'}`);
    renderDetails(PANEL_IDS.sync, []);
    return;
  }
  renderDetails(PANEL_IDS.sync, [
    ['Status', data.continuous_running ? 'RUNNING' : 'STOPPED'],
    ['PID', data.continuous_pid],
    ['Sync interval', formatDuration(data.interval_sec)],
    ['Live status interval', formatDuration(data.status_interval_sec)],
    ['Last sync', data.last_sync],
    ['Extraction method', data.extraction_method],
    ['Clients', data.total_clients],
    ['Source', data.source],
  ]);
  setPanel('sync', 'ok', 'Status only. Start/stop controls are intentionally absent.');
}

function renderAiFix(state) {
  const data = state.aiFix;
  if (!data) {
    setPanel('aiFix', 'error', `AI-fix read unavailable: ${state.errors.aiFix || 'missing source'}`);
    renderDetails(PANEL_IDS.aiFix, []);
    return;
  }
  const watcher = data.watcher || {};
  const pending = Array.isArray(data.pending) ? data.pending : [];
  const done = Array.isArray(data.recent_done) ? data.recent_done : [];
  const failed = Array.isArray(data.recent_failed) ? data.recent_failed : [];
  renderDetails(PANEL_IDS.aiFix, [
    ['Watcher', watcher.auto ? 'RUNNING' : 'STOPPED'],
    ['Pending', pending.length],
    ['Recent done', done.length],
    ['Recent failed', failed.length],
    ['Last action', watcher.last_action],
    ['Last model', watcher.last_model],
    ['CLI', boolLabel(watcher.cli_ok)],
    ['Runner', boolLabel(watcher.runner_ok)],
    ['Dry run', boolLabel(watcher.dry_run)],
    ['Watcher PID', watcher.pid],
    ['Models', Array.isArray(data.models) ? data.models.join(', ') : '-'],
  ]);
  setPanel('aiFix', 'ok', 'Queue and watcher metadata only. Queue actions are intentionally absent.');
}

function renderSettings(state) {
  const data = state.settings;
  if (!data) {
    setPanel('settings', 'error', `Public settings unavailable: ${state.errors.settings || 'missing source'}`);
    renderDetails(PANEL_IDS.settings, []);
    return;
  }
  renderDetails(PANEL_IDS.settings, [
    ['Tunnel port', data.tunnel_port],
    ['Auto restart tunnel', boolLabel(data.auto_restart_tunnel)],
    ['Auto Telegram', boolLabel(data.auto_telegram)],
    ['Auto open browser', boolLabel(data.auto_open_browser)],
  ]);
  setPanel('settings', 'ok', 'Redacted public projection. No settings write is available.');
}

function renderBackups(state) {
  const target = document.getElementById(PANEL_IDS.backups);
  if (!target) return;
  if (!state.backups) {
    setPanel('backups', 'error', `Backup list unavailable: ${state.errors.backups || 'missing source'}`);
    target.replaceChildren();
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 6;
    cell.className = 'empty-cell error-text';
    cell.textContent = 'Unable to read backup metadata.';
    row.appendChild(cell);
    target.appendChild(row);
    return;
  }
  const backups = Array.isArray(state.backups.backups) ? state.backups.backups : [];
  replaceChildren(target, backups.map((backup) => {
    const row = document.createElement('tr');
    [backup.name, backup.label || '-', formatTime(backup.created_at || backup.mtime), formatBytes(backup.size),
      Array.isArray(backup.files) ? backup.files.length : 0,
      Array.isArray(backup.script_files) ? backup.script_files.length : 0].forEach((value) => {
      const cell = document.createElement('td');
      cell.textContent = valueOrDash(value);
      row.appendChild(cell);
    });
    return row;
  }));
  if (!backups.length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 6;
    cell.className = 'empty-cell';
    cell.textContent = 'No backups reported by the golden source.';
    row.appendChild(cell);
    target.appendChild(row);
  }
  setPanel('backups', 'ok', `${backups.length} backup${backups.length === 1 ? '' : 's'} read from the golden metadata endpoint.`);
}

export function showBanner(message) {
  const banner = document.getElementById('connectionBanner');
  if (!banner) return;
  banner.hidden = !message;
  banner.textContent = message || '';
}

export function render(state) {
  renderContext(state);
  renderCards(state);
  renderCycle(state);
  renderSync(state);
  renderAiFix(state);
  renderSettings(state);
  renderBackups(state);
  const refreshed = document.getElementById('lastRefresh');
  if (refreshed && state.refreshedAt) refreshed.textContent = `Last refresh ${formatTime(state.refreshedAt)}`;
}
