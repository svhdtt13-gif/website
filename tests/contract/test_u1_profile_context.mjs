import assert from 'node:assert/strict';
import { panelContext } from '../../webapp/frontend/js/profile_context.js';
import { cycleControlBlocked, cycleStopRequested, syncReadAllowed } from '../../webapp/frontend/js/runtime_policy.js';

const profileA = {
  host_id: 'host-a',
  profile_id: 'profile-a',
  remote_account: 'account-a',
  verified_identity: 'identity-a',
  binding_id: 'binding-a',
};
const profileB = {
  host_id: 'host-b',
  profile_id: 'profile-b',
  remote_account: 'account-b',
  verified_identity: 'identity-b',
  binding_id: 'binding-b',
};

const missingPeer = panelContext({ cycle: { profile_context: profileA }, cycleSimple: {} }, ['cycle', 'cycleSimple']);
assert.equal(missingPeer.scope, 'UNSCOPED READ');
assert.equal(missingPeer.suppress, false);

const conflicting = panelContext({ cycle: { profile_context: profileA }, cycleSimple: { profile_context: profileB } }, ['cycle', 'cycleSimple']);
assert.equal(conflicting.scope, 'CONTEXT CONFLICT / FAIL CLOSED');
assert.equal(conflicting.suppress, true);

const accountConflict = panelContext({
  cycle: { profile_context: profileA },
  cycleSimple: { profile_context: { ...profileA, remote_account: 'account-other' } },
}, ['cycle', 'cycleSimple']);
assert.equal(accountConflict.scope, 'CONTEXT CONFLICT / FAIL CLOSED');
assert.equal(accountConflict.suppress, true);

const stoppedCycle = { stop_flag: true, cycle_running: false };
assert.equal(cycleStopRequested(stoppedCycle, {}), true);
assert.equal(cycleControlBlocked(stoppedCycle, {}), true);
assert.equal(syncReadAllowed({ continuous_running: false }), true);

console.log('PASS: U1 profile/account provenance and cycle-stop regressions');
