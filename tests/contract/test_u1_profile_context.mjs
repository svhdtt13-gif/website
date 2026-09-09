import assert from 'node:assert/strict';
import { panelContext } from '../../webapp/frontend/js/profile_context.js';
import { cycleControlBlocked, cycleStopRequested, syncReadAllowed } from '../../webapp/frontend/js/runtime_policy.js';

const profileA = {
  host_id: 'host-a',
  profile_id: 'profile-a',
  verified_identity: 'identity-a',
  binding_id: 'binding-a',
};
const profileB = {
  host_id: 'host-b',
  profile_id: 'profile-b',
  verified_identity: 'identity-b',
  binding_id: 'binding-b',
};

const missingPeer = panelContext({ cycle: { profile_context: profileA }, cycleSimple: {} }, ['cycle', 'cycleSimple']);
assert.equal(missingPeer.scope, 'UNSCOPED READ');
assert.equal(missingPeer.suppress, false);

const conflicting = panelContext({ cycle: { profile_context: profileA }, cycleSimple: { profile_context: profileB } }, ['cycle', 'cycleSimple']);
assert.equal(conflicting.scope, 'CONTEXT CONFLICT / FAIL CLOSED');
assert.equal(conflicting.suppress, true);

const stoppedCycle = { stop_flag: true, cycle_running: false };
assert.equal(cycleStopRequested(stoppedCycle, {}), true);
assert.equal(cycleControlBlocked(stoppedCycle, {}), true);
assert.equal(syncReadAllowed({ continuous_running: false }), true);

console.log('PASS: U1 profile provenance and cycle-stop regressions');
