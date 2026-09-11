import { refresh as refreshHosts } from './host_discovery.js';
import { refresh as refreshProfiles } from './profile_manager.js';

const HOST_REGISTRATION_ENDPOINT = '/up/api/host_registration';
const HOST_BINDING_ENDPOINT = '/up/api/host_binding';
const CONFIGURATION_STYLE = 'css/host_configuration.css';
const MUTATION_FAILURE = 'ERROR — configuration write failed';
const PROJECTIONS_REFRESHED = 'SUCCESS — write confirmed, projections refreshed';
const PROJECTIONS_STALE = 'WARNING/STALE — write confirmed, projection refresh failed/stale';

function addStyle() {
  if (document.querySelector(`link[href="${CONFIGURATION_STYLE}"]`)) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = CONFIGURATION_STYLE;
  document.head.appendChild(link);
}

function mount() {
  if (document.getElementById('hostConfiguration')) return;
  const anchor = document.getElementById('connectionBanner') || document.querySelector('main');
  if (!anchor) return;
  anchor.insertAdjacentHTML('beforebegin', `
    <section id="hostConfiguration" class="panel host-configuration-panel" aria-labelledby="hostConfigurationTitle">
      <div class="panel-heading">
        <div><p class="panel-kicker">Host Configuration / locked foundation</p><h2 id="hostConfigurationTitle">Configured identity and OFFLINE binding</h2></div>
        <span class="health health-ok">NO RUNTIME EFFECT</span>
      </div>
      <div class="configuration-safety" role="note"><strong>Runtime boundary</strong><span>These writes do not activate, start, stop, reconnect, log in, or change the legacy runtime owner.</span></div>
      <div class="configuration-grid">
        <form id="hostRegistrationForm" class="configuration-form" novalidate>
          <div class="configuration-form-heading"><h3>Register configured host</h3><p>Create the configured host identity only. Server-owned runtime effect remains <code>NONE</code>.</p></div>
          <div class="field-group"><label for="registrationHostId">Host ID</label><input id="registrationHostId" name="host_id" type="text" required pattern=".*\\S.*" autocomplete="off" aria-describedby="registrationHostIdHelp registrationError" /><p id="registrationHostIdHelp" class="field-help">Use the exact stable ID from explicit host configuration.</p></div>
          <div class="field-group"><label for="registrationDisplayName">Display name</label><input id="registrationDisplayName" name="display_name" type="text" required pattern=".*\\S.*" autocomplete="off" aria-describedby="registrationDisplayNameHelp registrationError" /><p id="registrationDisplayNameHelp" class="field-help">A readable operator label; it does not identify a runtime owner.</p></div>
          <dl class="locked-values" aria-label="Server-owned registration values"><div><dt>Runtime effect</dt><dd>NONE</dd></div><div><dt>Runtime authority</dt><dd>LEGACY</dd></div></dl>
          <p id="registrationError" class="form-message form-error" role="alert" hidden></p><button class="primary-action" type="submit" disabled>Review registration</button>
        </form>
        <form id="profileBindingForm" class="configuration-form" novalidate>
          <div class="configuration-form-heading"><h3>Create OFFLINE binding</h3><p>Associate one profile with one configured host without claiming runtime ownership.</p></div>
          <div class="field-group"><label for="bindingHostId">Host ID</label><input id="bindingHostId" name="host_id" type="text" required pattern=".*\\S.*" autocomplete="off" aria-describedby="bindingHostIdHelp bindingError" /><p id="bindingHostIdHelp" class="field-help">The host must already be configured or registered.</p></div>
          <div class="field-group"><label for="bindingProfileId">Profile ID</label><input id="bindingProfileId" name="profile_id" type="text" required pattern=".*\\S.*" autocomplete="off" aria-describedby="bindingProfileIdHelp bindingError" /><p id="bindingProfileIdHelp" class="field-help">Use an existing portable profile ID.</p></div>
          <dl class="locked-values" aria-label="Server-owned binding values"><div><dt>Binding state</dt><dd>OFFLINE</dd></div><div><dt>Runtime effect</dt><dd>NONE</dd></div><div><dt>Runtime authority</dt><dd>LEGACY</dd></div></dl>
          <p id="bindingError" class="form-message form-error" role="alert" hidden></p><button class="primary-action" type="submit" disabled>Review OFFLINE binding</button>
        </form>
      </div>
      <div id="configurationStatus" class="configuration-result" role="status" aria-live="polite">No configuration write has been submitted.</div>
    </section>
    <dialog id="configurationConfirmation" class="confirmation-dialog" aria-labelledby="confirmationTitle" aria-describedby="confirmationBoundary"><form method="dialog" class="confirmation-dialog-content"><p class="panel-kicker">Confirm safe write</p><h2 id="confirmationTitle">Review configuration</h2><p id="confirmationBoundary">Confirm the exact values below. Server-owned safety fields will be verified from the response.</p><dl id="confirmationDetails" class="confirmation-details"></dl><div class="dialog-actions"><button type="submit" value="cancel" class="secondary-action">Cancel</button><button id="confirmConfiguration" type="submit" value="confirm" class="primary-action">Confirm write</button></div></form></dialog>
  `);
}

function value(form, name) {
  return String(new FormData(form).get(name) || '');
}

function authorizationHeader() {
  const operatorToken = window.prompt('Operator write token');
  return operatorToken ? `Bearer ${operatorToken}` : null;
}

function setMessage(element, message) {
  element.textContent = message;
  element.hidden = !message;
}

function responseIsSafe(data, request) {
  if (!data || data.ok !== true || data.operation !== request.operation) return false;
  if (data.runtime_effect !== 'NONE' || data.runtime_authority?.mode !== 'LEGACY') return false;
  if (data.runtime_authority?.active_runtime_owner !== null) return false;
  if (data.result?.host_id !== request.payload.host_id) return false;
  if (request.operation === 'offline_binding') {
    return data.result?.profile_id === request.payload.profile_id && data.result?.state === 'OFFLINE';
  }
  return data.result?.display_name === request.payload.display_name;
}

async function post(request) {
  const authorization = authorizationHeader();
  const response = await fetch(request.endpoint, {
    method: 'POST', cache: 'no-store', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(authorization ? { Authorization: authorization } : {}) }, body: JSON.stringify(request.payload),
  });
  let data;
  try { data = await response.json(); } catch (_error) { throw new Error('Malformed JSON response.'); }
  if (!response.ok) throw new Error(`HTTP ${response.status}${data?.error ? `: ${data.error}` : '.'}`);
  if (!responseIsSafe(data, request)) throw new Error('Unsafe configuration response.');
}

async function refreshProjections() {
  const results = await Promise.allSettled([refreshProfiles(), refreshHosts()]);
  return results.every((result) => result.status === 'fulfilled' && result.value === true);
}

function showConfirmation(request) {
  const dialog = document.getElementById('configurationConfirmation');
  const title = document.getElementById('confirmationTitle');
  const details = document.getElementById('confirmationDetails');
  const confirm = document.getElementById('confirmConfiguration');
  title.textContent = request.title;
  details.replaceChildren();
  request.summary.forEach(([label, text]) => {
    const item = document.createElement('div'); const term = document.createElement('dt'); const valueNode = document.createElement('dd');
    term.textContent = label; valueNode.textContent = text; item.append(term, valueNode); details.append(item);
  });
  confirm.disabled = false; dialog.returnValue = ''; dialog.showModal();
  dialog.addEventListener('close', function onClose() {
    dialog.removeEventListener('close', onClose);
    if (dialog.returnValue !== 'confirm') return;
    submit(request);
  });
}

async function submit(request) {
  const status = document.getElementById('configurationStatus');
  const error = document.getElementById(request.kind === 'registration' ? 'registrationError' : 'bindingError');
  status.dataset.state = 'pending'; status.textContent = `Submitting ${request.kind} request...`;
  try {
    await post(request);
  } catch (reason) {
    const message = `${MUTATION_FAILURE}: ${reason.message || 'unknown error'}`;
    status.dataset.state = 'error'; status.textContent = message; setMessage(error, message);
    return;
  }
  if (!await refreshProjections()) {
    status.dataset.state = 'warning'; status.textContent = PROJECTIONS_STALE;
    setMessage(error, '');
    return;
  }
  status.dataset.state = 'success'; status.textContent = PROJECTIONS_REFRESHED;
  setMessage(error, '');
}

function wireForm(form, errorId, buildRequest) {
  const button = form.querySelector('button[type="submit"]'); const error = document.getElementById(errorId);
  const update = () => { button.disabled = !form.checkValidity(); };
  form.addEventListener('input', update); form.addEventListener('submit', (event) => { event.preventDefault(); if (!form.checkValidity()) { setMessage(error, 'Complete every required field before reviewing this request.'); return; } showConfirmation(buildRequest(form)); }); update();
}

function init() {
  addStyle(); mount();
  const registration = document.getElementById('hostRegistrationForm'); const binding = document.getElementById('profileBindingForm');
  if (!registration || !binding) return;
  wireForm(registration, 'registrationError', (form) => { const hostId = value(form, 'host_id'); const displayName = value(form, 'display_name'); return { kind: 'registration', label: 'Host registration', title: 'Confirm configured host registration', operation: 'host_registration', endpoint: HOST_REGISTRATION_ENDPOINT, payload: { host_id: hostId, display_name: displayName }, summary: [['Operation', 'Register configured host'], ['Host ID', hostId], ['Display name', displayName], ['Runtime effect', 'Verify NONE from response'], ['Runtime authority', 'Verify LEGACY from response']] }; });
  wireForm(binding, 'bindingError', (form) => { const hostId = value(form, 'host_id'); const profileId = value(form, 'profile_id'); return { kind: 'binding', label: 'OFFLINE binding', title: 'Confirm OFFLINE profile binding', operation: 'offline_binding', endpoint: HOST_BINDING_ENDPOINT, payload: { host_id: hostId, profile_id: profileId }, summary: [['Operation', 'Create profile binding'], ['Host ID', hostId], ['Profile ID', profileId], ['Binding state', 'Verify OFFLINE from response'], ['Runtime effect', 'Verify NONE from response'], ['Runtime authority', 'Verify LEGACY from response']] }; });
}

init();
