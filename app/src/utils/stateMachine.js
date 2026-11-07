/* Card-owned state topology, shared by every device page and home summary. */
function stateMachineRegions(machine) {
  if (!machine) return [];
  if (Array.isArray(machine.regions) && machine.regions.length) return machine.regions;
  return [{ ...machine, id: 'device', label: '设备', state_field: machine.state_field }];
}
function primaryStateRegion(machine) {
  const regions = stateMachineRegions(machine);
  return regions.find(region => region.id === machine?.primary_region)
    || regions.find(region => region.state_field === machine?.state_field)
    || regions[regions.length - 1] || null;
}
function stateRegionActive(region, regions, state, visited = new Set()) {
  if (!region?.parent) return true;
  if (visited.has(region.id)) return false;
  visited.add(region.id);
  const parent = regions.find(item => item.id === region.parent.region);
  return !!parent && state[parent.state_field] === region.parent.state
    && stateRegionActive(parent, regions, state, visited);
}
function stateRegionLabel(region, state) {
  const value = state?.[region?.state_field];
  return region?.views?.[value]?.label || (value == null || value === '' ? '未启用' : String(value));
}
function stateMachineSummary(machine, state = {}) {
  const regions = stateMachineRegions(machine);
  return regions.filter(region => stateRegionActive(region, regions, state))
    .map(region => stateRegionLabel(region, state)).filter(label => label !== '未启用').join(' · ')
    || '状态待同步';
}
function stateActionReason(machine, region, transition, state, { offline, busy, actor, stale } = {}) {
  if (offline) return '请先连接设备';
  if (transition.required_actor && transition.required_actor !== actor) return '需要管理员操作';
  if (transition.safety) return '';
  if (stale) return '状态连接正在恢复，等待最新状态';
  if (busy) return '正在等待本次执行结果';
  const key = transition.availability_key || transition.action;
  const blocked = state?.[transition.dispatch_only ? region.manual_blocked_actions_field : region.blocked_actions_field]?.[key];
  if (blocked) {
    const message = typeof blocked === 'object' ? blocked.message || blocked.reason || '当前不可用' : blocked;
    return machine?.reason_labels?.[message] || message;
  }
  const available = state?.[transition.dispatch_only ? region.manual_available_actions_field : region.available_actions_field];
  if (transition.dispatch_only && !Array.isArray(available)) return '正在同步可用动作';
  if (Array.isArray(available) && !available.includes(key)) return '当前状态暂不可用';
  if (!transition.dispatch_only && region.id === machine?.primary_region && machine.freshness_field && state?.[machine.freshness_field] === false) {
    return '设备反馈已过期，等待最新状态';
  }
  return '';
}
function stateActionVisible(region, transition, state) {
  if (!transition.available_only) return true;
  const available = state?.[region.available_actions_field];
  return Array.isArray(available) && available.includes(transition.availability_key || transition.action);
}
function stateWidgetEnabled(widget, state) {
  if (widget._disabled) return false;
  if (widget.enabled_when && !Object.entries(widget.enabled_when).every(([key, expected]) =>
    Array.isArray(expected) ? expected.includes(state?.[key]) : state?.[key] === expected)) return false;
  const available = state?.[widget.available_actions_field];
  return !Array.isArray(available) || available.includes(widget.availability_key || widget.action);
}
/* Clicking a target requests one declared, currently available transition.
 * Observation-only edges and indirect routes never become commands. */
function stateNodeControl(node, edges) {
  const incoming = edges.filter(edge => edge.dst === node.id && edge.transition.action);
  const active = incoming.filter(edge => edge.active);
  const available = active.filter(edge => !edge.reason);
  return {
    transition: !node.current && available.length === 1 ? available[0].transition : null,
    choices: active,
    reason: node.current ? '当前状态' : available.length > 1 ? '有多种转换方式，请选择下方动作'
      : available.length === 1 ? '' : active.find(edge => edge.reason)?.reason
        || '当前状态不能直接切换到' + node.label,
  };
}
/* Fold presentation-only parents into an observed child. Commands and gating
 * still belong to their original region; connection stays in the page header. */
function stateBoardTopology(machine, state = {}) {
  const all = stateMachineRegions(machine);
  const regions = all.filter(region => region.id !== 'connection' && region.state_field !== 'connection_state');
  const primary = primaryStateRegion(machine);
  const children = (region, value) => regions.filter(child => child.parent?.region === region.id && child.parent.state === value);
  const key = (region, value) => `${region.id}:${value}`;
  const anchor = (region, value, visited = new Set()) => {
    if (visited.has(region.id)) return null;
    visited.add(region.id);
    const nested = children(region, value);
    if (!nested.length) return key(region, value);
    const child = nested.find(item => region.diagram_group && item.diagram_group === region.diagram_group)
      || nested.find(item => item.id === primary?.id) || nested[0];
    return anchor(child, child.states.includes(state[child.state_field]) ? state[child.state_field] : child.states[0], visited);
  };
  const nodes = regions.flatMap(region => region.states.filter(value => !children(region, value).length)
    .map(value => ({id:key(region,value), value, region, view:region.views?.[value], label:region.views?.[value]?.label || value,
      layout:region.layout?.[value], group:region.diagram_group, radius:region.node_radius,
      current:state[region.state_field] === value && stateRegionActive(region, all, state),
      retained:state[region.state_field] === value && region.retain_when_inactive && !stateRegionActive(region, all, state)})));
  const ids = new Set(nodes.map(node => node.id));
  const edges = regions.flatMap(region => (region.transitions || [])
    .filter(edge => !edge.safety && !edge.graph_hidden && stateActionVisible(region, edge, state))
    .map((transition,index) => ({id:`${region.id}:${index}`,region,transition,
      src:anchor(region,transition.src),dst:anchor(region,transition.dst),
      active:stateRegionActive(region,all,state) && (transition.dispatch_only || state[region.state_field] === transition.src)})))
    .filter(edge => ids.has(edge.src) && ids.has(edge.dst));
  return {nodes,edges};
}
Object.assign(window, { stateMachineRegions, primaryStateRegion, stateRegionActive, stateRegionLabel,
  stateMachineSummary, stateActionReason, stateActionVisible, stateWidgetEnabled, stateNodeControl, stateBoardTopology });
