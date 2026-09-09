import { ENDPOINTS, getJSON } from './api.js';

export const store = {
  cycle: null,
  cycleSimple: null,
  sync: null,
  general: null,
  aiFix: null,
  backups: null,
  settings: null,
  errors: {},
  refreshedAt: null,
};

export async function refreshStore() {
  const entries = Object.entries(ENDPOINTS);
  const results = await Promise.allSettled(entries.map(([, endpoint]) => getJSON(endpoint)));
  const errors = {};
  entries.forEach(([key], index) => {
    const result = results[index];
    if (result.status === 'fulfilled') store[key] = result.value;
    else {
      store[key] = null;
      errors[key] = result.reason instanceof Error ? result.reason.message : 'Unavailable';
    }
  });
  store.errors = errors;
  store.refreshedAt = new Date().toISOString();
  return store;
}
