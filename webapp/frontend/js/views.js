// Render DOM thuần từ state (không gọi mạng ở đây).
export function showBanner(msg) {
  const b = document.getElementById('banner');
  if (!msg) { b.style.display = 'none'; b.textContent = ''; return; }
  b.style.display = 'block'; b.textContent = msg;
}
export function setText(id, text, cls) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = cls || '';
}
export function renderCards(s) {
  const cycle = s.cycle || {};
  const sync = s.sync || {};
  const ai = s.ai || {};
  setText('cardCycle', cycle.cycle_running ? ('Running (pid ' + cycle.cycle_pid + ')') : 'Offline', cycle.cycle_running ? 'ok' : 'bad');
  setText('cardSync', sync.continuous_running ? 'Running' : 'Stopped', sync.continuous_running ? 'ok' : 'bad');
  setText('cardProc', '360Auto ' + (cycle['360auto'] || 0) + ' • qnyh ' + (cycle.qnyh || 0));
  const w = ai.watcher || {};
  setText('cardAi', (w.auto ? 'Tự động BẬT' : 'Tự động TẮT') + ' • chờ ' + (((ai && ai.pending) || []).length), w.auto ? 'ok' : 'warn');
}
export function renderClients(master) {
  const clients = (master && master.clients) || [];
  document.getElementById('clientCount').textContent = clients.length;
  const tb = document.getElementById('clientBody');
  tb.innerHTML = '';
  clients.forEach(function (c) {
    const tr = document.createElement('tr');
    [[c.client], [c.name], [c.group], [c.status]].forEach(function (v, i) {
      const td = document.createElement('td');
      td.textContent = v[0] == null ? '—' : String(v[0]);
      if (i === 3) td.className = (c.status === 'running') ? 'ok' : 'bad';
      tr.appendChild(td);
    });
    tb.appendChild(tr);
  });
  if (!clients.length) tb.innerHTML = '<tr><td colspan="4">Trống.</td></tr>';
}
export function render(s) {
  renderCards(s);
  renderClients(s.master);
  const overrides = ((s.cycle || {}).manual_overrides || []);
  if (overrides.length) {
    showBanner('⚠ Thao tác tay trên remote: ' + overrides.map(function (m) { return m.client + ' (' + m.change + ')'; }).join(' • '));
  } else showBanner('');
}
