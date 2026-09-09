export function cycleStopRequested(cycle = {}, simple = {}) {
  return cycle.stop_flag === true || simple.stopped === true;
}

export function cycleControlBlocked(cycle = {}, simple = {}) {
  return cycleStopRequested(cycle, simple);
}

// A cycle stop intent does not disable read-only remote observation/reconciliation.
export function syncReadAllowed(sync) {
  return sync !== null && sync !== undefined;
}
