const ENDPOINT = '/up/api/host_discovery';

function text(value) {
  return value === undefined || value === null || value === '' ? '-' : String(value);
}

function detail(parent, label, value) {
  const item = document.createElement('div');
  item.className = 'detail-item';
  const title = document.createElement('dt');
  title.textContent = label;
  const content = document.createElement('dd');
  content.textContent = text(value);
  item.append(title, content);
  parent.appendChild(item);
}

function clearView(message) {
  const status = document.getElementById('hostDiscoveryStatus');
  const identity = document.getElementById('hostIdentityDetails');
  const binding = document.getElementById('hostBindingDetails');
  const prerequisites = document.getElementById('hostPrerequisiteBody');
  const owner = document.getElementById('hostRuntimeOwner');
  if (status) status.textContent = message;
  if (identity) identity.replaceChildren();
  if (binding) binding.replaceChildren();
  if (prerequisites) prerequisites.replaceChildren();
  if (owner) owner.textContent = 'UNOBSERVED / LEGACY AUTHORITY';
}

function render(data) {
  const status = document.getElementById('hostDiscoveryStatus');
  const identity = document.getElementById('hostIdentityDetails');
  const binding = document.getElementById('hostBindingDetails');
  const prerequisites = document.getElementById('hostPrerequisiteBody');
  const owner = document.getElementById('hostRuntimeOwner');
  if (!status || !identity || !binding || !prerequisites || !owner) return;

  const configured = data.configured_host_identity || {};
  const portable = data.portable_binding || {};
  const current = portable.current_binding || {};
  const observation = data.local_runtime_observation || {};
  detail(identity, 'Host ID', configured.host_id || 'NOT CONFIGURED');
  detail(identity, 'Identity source', configured.source || 'explicit_config');
  detail(identity, 'Hostname fallback', 'DISABLED');
  detail(binding, 'Binding status', portable.status || 'UNBOUND');
  detail(binding, 'Bound host', portable.host?.display_name || 'None');
  detail(binding, 'Binding ID', current.binding_id || 'None');
  detail(binding, 'Profile ID', current.profile_id || 'None');
  detail(binding, 'Profile status', current.profile_status || 'None');
  detail(binding, 'Binding state', current.state || 'None');
  owner.textContent = data.runtime_authority?.active_runtime_owner
    ? `${text(data.runtime_authority.active_runtime_owner.profile_id)} / ${text(data.runtime_authority.active_runtime_owner.host_id)}`
    : 'UNOBSERVED / LEGACY AUTHORITY';
  status.textContent = `${Array.isArray(observation.prerequisites) ? observation.prerequisites.length : 0} allowlisted prerequisites observed. No runtime owner inferred.`;

  prerequisites.replaceChildren();
  (observation.prerequisites || []).forEach((item) => {
    const row = document.createElement('tr');
    [item.name, item.path, item.state, item.read_only_probe ? 'YES' : 'NO'].forEach((value) => {
      const cell = document.createElement('td');
      cell.textContent = text(value);
      row.appendChild(cell);
    });
    prerequisites.appendChild(row);
  });
}

async function refresh() {
  try {
    const response = await fetch(ENDPOINT, { cache: 'no-store', credentials: 'same-origin' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (data.read_only !== true || data.runtime_authority?.active_runtime_owner !== null) {
      throw new Error('unsafe host discovery response');
    }
    render(data);
  } catch (error) {
    clearView(`Host discovery unavailable: ${error.message || 'unknown error'}`);
  }
}

refresh();
setInterval(refresh, 10000);
