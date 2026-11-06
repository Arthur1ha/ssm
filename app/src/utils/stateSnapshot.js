/* One cursor per consumer: reconnects may restart revision, retired epochs may not return. */
function mergeStateSnapshot(current, incoming, retiredEpochs = new Set()) {
  if (!incoming || typeof incoming !== 'object' || !Object.keys(incoming).length) return current;
  if (!current || !Object.keys(current).length) return incoming;
  if (current.epoch && incoming.epoch) {
    if (current.epoch === incoming.epoch) {
      return Number(incoming.revision) < Number(current.revision) ? current : incoming;
    }
    if (retiredEpochs.has(incoming.epoch)) return current;
    retiredEpochs.add(current.epoch);
    return incoming;
  }
  if (current.epoch && !incoming.epoch) return current;
  if (incoming.epoch) return incoming;
  // Physical observation time and cloud control time are independent cursors.
  // A new device frame may omit the overlay; a mode event may reuse physical ts.
  const controlFields = ['autonomy_mode', 'control_state', 'control_hold_until', 'control_ts'];
  const physical = (current.ts || 0) > (incoming.ts || 0) ? current : incoming;
  const oldControlTime = Number(current.control_ts) || 0;
  const newControlTime = Number(incoming.control_ts) || 0;
  const control = newControlTime > oldControlTime ? incoming : current;
  const result = { ...physical };
  controlFields.forEach(key => {
    if (Object.prototype.hasOwnProperty.call(control, key)) result[key] = control[key];
    else if (!oldControlTime && !newControlTime && Object.prototype.hasOwnProperty.call(incoming, key)) result[key] = incoming[key];
  });
  return result;
}
window.mergeStateSnapshot = mergeStateSnapshot;
