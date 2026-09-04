// Lớp HTTP: mọi dữ liệu qua proxy cùng origin (/up/*).
// Giữ nguyên contract: GET only, no-store, ném Error 'HTTP <status>' khi lỗi.
export async function getJSON(path) {
  const r = await fetch(path, { cache: 'no-store' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}
export const ENDPOINTS = {
  cycle: '/up/api/cycle/status',
  sync: '/up/api/sync_status',
  master: '/up/clients_master.json',
  aiFix: '/up/api/ai_fix/status',
};
