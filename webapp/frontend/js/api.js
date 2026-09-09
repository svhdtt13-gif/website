// U1 is deliberately GET-only. Every source is an existing Phase 2 read boundary.
export const ENDPOINTS = {
  cycle: '/up/api/cycle/status',
  cycleSimple: '/up/api/cycle_status',
  sync: '/up/api/sync_status',
  general: '/up/api/status',
  aiFix: '/up/api/ai_fix/status',
  backups: '/up/api/cycle/backup',
  settings: '/up/api/settings',
};

export async function getJSON(path) {
  const response = await fetch(path, { cache: 'no-store', credentials: 'same-origin' });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const data = await response.json();
  if (data && typeof data === 'object' && data.error) throw new Error(String(data.error));
  return data;
}
