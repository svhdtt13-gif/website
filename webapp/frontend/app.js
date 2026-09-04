/* Dashboard read-only: mọi dữ liệu qua proxy cùng origin (/up/*). */
async function getJSON(path) {
  const r = await fetch(path, { cache: 'no-store' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}
function showBanner(msg) {
  const b = document.getElementById('banner');
  if (!msg) { b.style.display = 'none'; b.textContent = ''; return; }
  b.style.display = 'block'; b.textContent = msg;
}
function setText(id, text, cls) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = cls || '';
}
async function refresh() {
  try {
    const [cycle, sync, master, ai] = await Promise.all([
      getJSON('/up/api/cycle/status'),
      getJSON('/up/api/sync_status'),
      getJSON('/up/clients_master.json'),
      getJSON('/up/api/ai_fix/status'),
    ]);
    setText('cardCycle', cycle.cycle_running ? ('Running (pid ' + cycle.cycle_pid + ')') : 'Offline', cycle.cycle_running ? 'ok' : 'bad');
    setText('cardSync', sync.continuous_running ? 'Running' : 'Stopped', sync.continuous_running ? 'ok' : 'bad');
    setText('cardProc', '360Auto ' + (cycle['360auto'] || 0) + ' • qnyh ' + (cycle.qnyh || 0));
    const w = (ai && ai.watcher) || {};
    setText('cardAi', (w.auto ? 'Tự động BẬT' : 'Tự động TẮT') + ' • chờ ' + (((ai && ai.pending) || []).length), w.auto ? 'ok' : 'warn');
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
    if ((cycle.manual_overrides || []).length) {
      showBanner('⚠ Thao tác tay trên remote: ' + cycle.manual_overrides.map(function (m) { return m.client + ' (' + m.change + ')'; }).join(' • '));
    } else showBanner('');
  } catch (e) {
    showBanner('Không lấy được dữ liệu từ ai tool (' + e.message + '). Kiểm tra proxy + ai tool :8080.');
  }
}
refresh();
setInterval(refresh, 10000);
