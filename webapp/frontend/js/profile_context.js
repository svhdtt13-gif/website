const FIELD_ALIASES = {
  hostId: ['host_id', 'hostId'],
  profileId: ['profile_id', 'profileId'],
  account: ['remote_account', 'remoteAccount', 'account_ref'],
  identity: ['verified_identity', 'verifiedIdentity', 'identity_ref'],
  binding: ['active_binding_id', 'binding_id', 'binding_generation'],
  bindingState: ['binding_state', 'bindingState'],
};

function readField(context, names) {
  for (const name of names) {
    if (context[name] !== undefined && context[name] !== null && context[name] !== '') return context[name];
  }
  return null;
}

function extractContext(data) {
  const raw = data?.profile_context || data?.context;
  if (!raw || typeof raw !== 'object') {
    return { present: false, complete: false, values: {} };
  }
  const values = Object.fromEntries(Object.entries(FIELD_ALIASES)
    .map(([key, aliases]) => [key, readField(raw, aliases)]));
  const verified = values.identity !== null
    && !['UNVERIFIED', 'unverified', 'unknown'].includes(String(values.identity));
  const bound = values.binding !== null || values.bindingState === 'ACTIVE';
  return {
    present: true,
    complete: Boolean(values.hostId && values.profileId && verified && bound),
    values,
  };
}

function conflicts(contexts) {
  const keys = ['hostId', 'profileId', 'identity', 'binding'];
  return contexts.some((left, leftIndex) => contexts.slice(leftIndex + 1).some((right) => keys.some((key) => (
    left.values[key] !== null && right.values[key] !== null
      && String(left.values[key]) !== String(right.values[key])
  ))));
}

export function panelContext(state, sourceKeys) {
  const contexts = sourceKeys.map((key) => extractContext(state[key]));
  if (conflicts(contexts.filter((context) => context.present))) {
    return {
      scope: 'CONTEXT CONFLICT / FAIL CLOSED',
      suppress: true,
      message: 'Conflicting host/profile context across this panel sources. Data suppressed.',
      context: null,
    };
  }
  if (!contexts.length || contexts.some((context) => !context.present || !context.complete)) {
    return {
      scope: 'UNSCOPED READ',
      suppress: false,
      message: 'This panel has no complete host/profile proof. It is read-only and not scoped to another panel.',
      context: null,
    };
  }
  return {
    scope: 'PROFILE-SCOPED READ',
    suppress: false,
    message: 'This panel has matching host/profile/identity/binding proof. U1 remains read-only.',
    context: contexts[0].values,
  };
}
