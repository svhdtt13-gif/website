// Bootstrap: khởi động + vòng polling 10s (giữ nguyên hành vi app.js cũ).
import { refreshStore, store } from './store.js';
import { render, showBanner } from './views.js';
async function refresh() {
  try {
    await refreshStore();
    render(store);
  } catch (e) {
    showBanner('Không lấy được dữ liệu từ ai tool (' + e.message + '). Kiểm tra proxy + ai tool :8080.');
  }
}
refresh();
setInterval(refresh, 10000);
