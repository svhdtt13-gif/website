const ENDPOINT = '/up/api/profile_manager';
const VIEWED_KEY = 'autoghoststory.viewedProfileId';

function readViewedProfile() {
  try {
    return window.localStorage.getItem(VIEWED_KEY) || '';
  } catch (_error) {
    return '';
  }
}

function writeViewedProfile(value) {
  try {
    if (value) window.localStorage.setItem(VIEWED_KEY, value);
    else window.localStorage.removeItem(VIEWED_KEY);
  } catch (_error) {
    // A viewed profile is optional browser-local state.
  }
}

function text(value) {
  return value === undefined || value === null || value === '' ? '-' : String(value);
}

function addDetail(parent, label, value) {
  const item = document.createElement('div');
  item.className = 'detail-item';
  const title = document.createElement('dt');
  title.textContent = label;
  const content = document.createElement('dd');
  content.textContent = text(value);
  item.append(title, content);
  parent.appendChild(item);
}

function renderError(message) {
  const status = document.getElementById('profileManagerStatus');
  const body = document.getElementById('profileManagerBody');
  const tableBody = document.getElementById('profileManagerBodyRows');
  const select = document.getElementById('profileViewSelect');
  const owner = document.getElementById('profileRuntimeOwner');
  if (status) status.textContent = message;
  if (body) body.replaceChildren();
  if (tableBody) tableBody.replaceChildren();
  if (select) {
    select.replaceChildren(new Option('Profile data unavailable', ''));
    select.value = '';
    select.disabled = true;
  }
  if (owner) owner.textContent = 'UNOBSERVED / LEGACY AUTHORITY';
}

function render(data) {
  const profiles = Array.isArray(data.profiles) ? data.profiles : [];
  const select = document.getElementById('profileViewSelect');
  const status = document.getElementById('profileManagerStatus');
  const detailBody = document.getElementById('profileManagerBody');
  const tableBody = document.getElementById('profileManagerBodyRows');
  const owner = document.getElementById('profileRuntimeOwner');
  if (!select || !status || !detailBody || !tableBody || !owner) return;
  select.disabled = false;

  let viewed = readViewedProfile();
  if (viewed && !profiles.some((profile) => profile.profile_id === viewed)) {
    viewed = '';
    writeViewedProfile('');
  }
  select.replaceChildren(new Option('No viewed profile', ''));
  profiles.forEach((profile) => select.appendChild(new Option(
    `${text(profile.display_name)} (${text(profile.profile_id)})`, profile.profile_id,
  )));
  select.value = viewed;
  select.onchange = () => {
    writeViewedProfile(select.value);
    render(data);
  };

  const runtimeOwner = data.runtime_authority?.active_runtime_owner;
  owner.textContent = runtimeOwner
    ? `${text(runtimeOwner.profile_id)} / ${text(runtimeOwner.host_id)}`
    : 'UNOBSERVED / LEGACY AUTHORITY';
  status.textContent = `${profiles.length} profile${profiles.length === 1 ? '' : 's'} observed. Controls disabled.`;

  const viewedProfile = profiles.find((profile) => profile.profile_id === viewed);
  const detail = document.createElement('div');
  detail.className = 'profile-view-detail detail-grid';
  addDetail(detail, 'Viewed profile', viewedProfile?.display_name || 'None');
  addDetail(detail, 'Viewed profile ID', viewedProfile?.profile_id || 'None');
  addDetail(detail, 'Status', viewedProfile?.status || 'None');
  addDetail(detail, 'Account reference', viewedProfile?.account_ref || 'None');
  addDetail(detail, 'Current binding', viewedProfile?.binding?.binding_id || 'None');
  addDetail(detail, 'Binding host', viewedProfile?.binding?.host_id || 'None');
  detailBody.replaceChildren(detail);

  tableBody.replaceChildren();
  profiles.forEach((profile) => {
    const row = document.createElement('tr');
    [profile.display_name, profile.profile_id, profile.status,
      profile.binding?.host_id || '-', profile.binding?.state || '-'].forEach((value) => {
      const cell = document.createElement('td');
      cell.textContent = text(value);
      row.appendChild(cell);
    });
    tableBody.appendChild(row);
  });
  if (!profiles.length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 5;
    cell.className = 'empty-cell';
    cell.textContent = 'No portable profiles are available.';
    row.appendChild(cell);
    tableBody.appendChild(row);
  }
}

export async function refresh() {
  try {
    const response = await fetch(ENDPOINT, { cache: 'no-store', credentials: 'same-origin' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (data.read_only !== true || data.controls?.can_switch !== false) {
      throw new Error('unsafe profile manager response');
    }
    render(data);
  } catch (error) {
    renderError(`Profile manager unavailable: ${error.message || 'unknown error'}`);
  }
}

refresh();
setInterval(refresh, 10000);
