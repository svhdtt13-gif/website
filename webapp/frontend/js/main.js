import { refreshStore, store } from './store.js';
import { render, showBanner } from './views.js';
import './host_configuration.js';

async function refresh() {
  await refreshStore();
  render(store);
  const errorCount = Object.keys(store.errors).length;
  showBanner(errorCount ? `${errorCount} read source${errorCount === 1 ? '' : 's'} unavailable. Panels remain read-only.` : '');
}

refresh().catch((error) => showBanner(`Dashboard refresh failed: ${error.message || 'unknown error'}`));
setInterval(() => refresh().catch((error) => showBanner(`Dashboard refresh failed: ${error.message || 'unknown error'}`)), 10000);
