/* Card-owned state topology, shared by every device page and home summary. */
function stateMachineRegions(machine) {
  return machine?.regions || [];
}
function primaryStateRegion(machine) {
  const regions = stateMachineRegions(machine);
  return regions.find(region => region.id === machine?.primary_region) || null;
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
  if (state?.[region.state_field] != null && state[region.state_field] !== transition.src) {
    return '请先切换到' + (region.views?.[transition.src]?.label || transition.src);
  }
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
  const incoming = edges.filter(edge => edge.dst === node.id);
  const automatic = incoming.filter(edge => edge.active && !edge.reason && !edge.transition.action);
  // Multiple source states may declare the same telemetry-independent command.
  const commands = new Map();
  for (const edge of incoming.filter(edge => edge.transition.action && edge.active && (!node.current
    || edge.transition.available_only))) {
    const key = JSON.stringify([edge.transition.action,
      Object.entries(edge.transition.params || {}).sort(([a],[b]) => a.localeCompare(b))]);
    if (!commands.has(key) || !edge.reason) commands.set(key,edge);
  }
  if (!node.current && ![...commands.values()].some(edge => !edge.reason)) {
    for (const edge of incoming.filter(edge => edge.preparedTransition)) {
      const key = JSON.stringify([edge.transition.action, edge.transition.params || {}]);
      commands.set(key, {...edge,active:true,reason:edge.preparationReason,
        transition:edge.preparedTransition});
    }
  }
  const active = [...commands.values()];
  const available = active.filter(edge => !edge.reason);
  return {
    transition: available.length === 1 ? available[0].transition : null,
    reachable: available.length > 0 || automatic.length > 0,
    automatic,
    choices: active,
    reason: available.length > 1 ? '请选择动作'
      : available.length === 1 ? '' : active.find(edge => edge.reason)?.reason
        || '当前状态不能直接切换到' + node.label,
  };
}
/* One physical line per unordered pair; keep every rule in its direction. */
function stateGraphLinks(edges) {
  const links = new Map();
  for (const edge of edges) {
    if (edge.src === edge.dst && !edge.hasTeaching) continue;
    const [src,dst] = [edge.src,edge.dst].sort();
    const id = JSON.stringify([src,dst]);
    if (!links.has(id)) links.set(id,{id,src,dst,directions:[]});
    const link = links.get(id), directionId = JSON.stringify([edge.src,edge.dst]);
    let direction = link.directions.find(item => item.id === directionId);
    if (!direction) { direction = {id:directionId,src:edge.src,dst:edge.dst,edges:[]}; link.directions.push(direction); }
    direction.edges.push(edge);
  }
  return [...links.values()];
}
function stateLinkDirection(link, nodes, selectedDirection) {
  return link.directions.find(direction => direction.id === selectedDirection)
    || link.directions.find(direction => nodes.some(node => node.id === direction.src && node.current))
    || link.directions.find(direction => direction.edges.some(edge => edge.active && !edge.reason))
    || link.directions[0];
}
function stateTransitionLabel(transition) {
  return ({action_completed:'动作执行完毕',motion_observed:'开始移动',motion_stationary:'停止移动',
    BLINK_DONE:'闪烁结束'})[transition.trigger]
    || (transition.label || '').replace(/^接纳/,'开始').replace(/ · 自动转换$/,'');
}
/* Small graphs need a shallower default canvas; explicit Card diagrams win. */
function stateGraphDimensions(nodeCount, diagram) {
  return {width: diagram?.width || 600,
    height: diagram?.height || (nodeCount <= 2 ? 200 : nodeCount <= 4 ? 320 : 600)};
}

/* Fold presentation-only parents into an observed child. Commands and gating
 * still belong to their original region; connection stays in the page header. */
function stateBoardTopology(machine, state = {}, visibleTransitionIds = new Set()) {
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
    .filter(edge => !edge.safety && (!edge.graph_hidden || visibleTransitionIds.has(edge.id))
      && stateActionVisible(region, edge, state))
    .map((transition,index) => ({id:`${region.id}:${index}`,region,transition,
      src:anchor(region,transition.src),dst:anchor(region,transition.dst),
      // Manual dispatch changes how we wait for results, not the source pose.
      // Keep explicit manual recovery available when no pose has been observed.
      active:stateRegionActive(region,all,state) && (state[region.state_field] === transition.src
        || transition.dispatch_only && state[region.state_field] == null)})))
    .filter(edge => ids.has(edge.src) && ids.has(edge.dst));
  return {nodes,edges};
}
Object.assign(window, { stateMachineRegions, primaryStateRegion, stateRegionActive, stateRegionLabel,
  stateMachineSummary, stateActionReason, stateActionVisible, stateWidgetEnabled, stateNodeControl, stateBoardTopology,
  stateGraphLinks, stateLinkDirection, stateTransitionLabel, stateGraphDimensions });
