// Kho state + điều phối tải dữ liệu (không đụng DOM).
import { getJSON, ENDPOINTS } from './api.js';
export const store = { cycle: null, sync: null, master: null, ai: null };
export async function refreshStore() {
  const [cycle, sync, master, ai] = await Promise.all([
    getJSON(ENDPOINTS.cycle),
    getJSON(ENDPOINTS.sync),
    getJSON(ENDPOINTS.master),
    getJSON(ENDPOINTS.aiFix),
  ]);
  store.cycle = cycle;
  store.sync = sync;
  store.master = master;
  store.ai = ai;
  return store;
}
