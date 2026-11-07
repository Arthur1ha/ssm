/* Non-modal card anchored in SVG space; never participates in canvas layout. */
function StateBoardPopover({ anchor, canvasRef, label, onClose, children }) {
  const ref = React.useRef(null);
  const reposition = React.useRef(null);
  React.useLayoutEffect(() => {
    const card = ref.current, canvas = canvasRef.current;
    if (!card || !canvas || !anchor?.element?.isConnected) return;
    const place = () => {
      const matrix = anchor.element.getScreenCTM();
      if (!matrix) return;
      const bounds = canvas.getBoundingClientRect();
      const screen = new DOMPoint(anchor.point.x,anchor.point.y).matrixTransform(matrix);
      const x = screen.x-bounds.left, y = screen.y-bounds.top;
      const w = card.offsetWidth, h = card.offsetHeight, gap = 16, margin = 8;
      const nodes = [...canvas.querySelectorAll('.fsm-whiteboard__node')].map(node => {
        const r = node.getBoundingClientRect();
        const related = [anchor.element.dataset.source,anchor.element.dataset.target,anchor.element.dataset.state].includes(node.dataset.state);
        return {left:r.left-bounds.left,top:r.top-bounds.top,right:r.right-bounds.left,bottom:r.bottom-bounds.top,related};
      });
      const related = nodes.filter(node => node.related);
      const candidates = [{x:x+gap,y:y-h/2},{x:x-w-gap,y:y-h/2},
        {x:x-w/2,y:y+gap},{x:x-w/2,y:y-h-gap},
        ...(related.length ? [{x:x-w/2,y:Math.min(...related.map(node => node.top))-h-8},
          {x:x-w/2,y:Math.max(...related.map(node => node.bottom))+8}] : [])].map(point => {
        const left = Math.max(margin,Math.min(point.x,bounds.width-w-margin));
        const top = Math.max(margin,Math.min(point.y,bounds.height-h-margin));
        const overlap = nodes.reduce((sum,r) => sum + Math.max(0,Math.min(left+w,r.right)-Math.max(left,r.left))
          * Math.max(0,Math.min(top+h,r.bottom)-Math.max(top,r.top)) * (r.related ? 8 : 1),0);
        return {left,top,score:overlap+Math.hypot(left-point.x,top-point.y)};
      });
      const best = candidates.reduce((a,b) => a.score<=b.score ? a : b);
      card.style.left = best.left+'px'; card.style.top = best.top+'px';
      card.style.visibility = 'visible';
    };
    reposition.current = place;
    place();
    const observer = new ResizeObserver(place);
    observer.observe(card); observer.observe(canvas);
    window.addEventListener('resize',place);
    const outside = event => { if (!canvas.contains(event.target)) onClose(); };
    const escape = event => {
      if (event.key !== 'Escape' || event.target.closest?.('[aria-modal="true"]')) return;
      event.stopPropagation(); anchor.element.focus({preventScroll:true}); onClose();
    };
    document.addEventListener('pointerdown',outside);
    document.addEventListener('keydown',escape);
    if (anchor.keyboard) card.focus({preventScroll:true});
    return () => { reposition.current = null; observer.disconnect(); window.removeEventListener('resize',place);
      document.removeEventListener('pointerdown',outside); document.removeEventListener('keydown',escape); };
  }, [anchor,canvasRef,onClose]);
  React.useLayoutEffect(() => { reposition.current?.(); });
  return <section ref={ref} className="device-whiteboard__popover" role="dialog" aria-label={label}
    tabIndex="-1" style={{visibility:'hidden'}}>{children}</section>;
}

function StateTransitionRules({ edges, records, error, card, onEdit, onRetry }) {
  const rules = [...new Map(edges.flatMap(edge => stateTransitionTeachings(edge.transition,records,card))
    .map(rule => [rule.teaching_id,rule])).values()];
  return <>
    {error && <div className="device-whiteboard__rule-error" role="status">规则读取失败 <button type="button" onClick={onRetry}>重试</button></div>}
    {rules.map(rule => <article key={rule.teaching_id} className="device-whiteboard__rule">
      <p>{ruleObservationCopy(rule.binding.observation_id,rule.binding.where)}</p>
      <p>→ {ruleActionCopy(rule.binding.skill_id,rule.binding.params,card)}</p>
      <footer><small>用户规则 · {({enabled:'已启用',ready:'未启用',unbound:'未绑定',capability_unavailable:'能力已变化'})[rule.execution_status] || '未启用'}</small>
        <button type="button" onClick={() => onEdit(rule.teaching_id)}>编辑规则</button></footer>
    </article>)}
  </>;
}

/* Shared flat whiteboard. Device Card remains the authority for all commands. */
function DeviceStateBoard({ unitId, sm, current, widgets = [], liveState = {}, connected,
  offline, actor, color, busy, onSelectState, onTransition, report, streamHealth, feedback, card }) {
  const { useState, useEffect, useRef, useCallback } = React;
  const [selectedId, setSelectedId] = useState(null);
  const [selectedLinkId, setSelectedLinkId] = useState(null);
  const [selectedDirectionId, setSelectedDirectionId] = useState(null);
  const [anchor,setAnchor] = useState(null);
  const [editingRule,setEditingRule] = useState(null);
  const canvasRef = useRef(null);
  const [ruleSnapshot,setRuleSnapshot] = useState(null);
  const ruleRequest = useRef(0);
  const canReadRules = actor === 'admin' && card?.services?.teachings_endpoint === '/api/teachings';
  const loadRules = useCallback(async () => {
    const request = ++ruleRequest.current;
    if (!canReadRules) { setRuleSnapshot(null); return; }
    try {
      const records = await apiClient.request(`/api/teachings?unit_id=${encodeURIComponent(unitId)}`);
      if (request === ruleRequest.current) setRuleSnapshot({unitId,records,error:false});
    } catch (_) {
      if (request === ruleRequest.current) setRuleSnapshot(previous => ({unitId,error:true,
        records:previous?.unitId === unitId ? previous.records : []}));
    }
  }, [unitId,canReadRules]);
  useEffect(() => { loadRules(); return () => { ++ruleRequest.current; }; }, [loadRules]);
  const rules = canReadRules && ruleSnapshot?.unitId === unitId ? ruleSnapshot.records : [];
  const clearSelection = useCallback(() => {
    setSelectedId(null); setSelectedLinkId(null); setSelectedDirectionId(null); setAnchor(null); onSelectState?.(null);
  }, [onSelectState]);
  const closePopover = useCallback(() => {
    if (canvasRef.current?.querySelector('.device-whiteboard__popover')?.contains(document.activeElement))
      anchor?.element?.focus({preventScroll:true});
    clearSelection();
  }, [anchor,clearSelection]);
  const [joystickExiting, setJoystickExiting] = useState(false);
  const nodeCommandPending = useRef(false);
  const primary = primaryStateRegion(sm);
  const stale = streamHealth === 'reconnecting';
  const topology = stateBoardTopology(sm, liveState);
  const pendingExecution = liveState[sm?.execution_field];
  const execution = pendingExecution || liveState[sm?.last_execution_field];
  const awaitingFeedback = connected && primary?.state_field && liveState[primary.state_field] == null;
  const remoteActive = widget => Object.keys(widget.enabled_when || {}).length > 0
    && Object.entries(widget.enabled_when).every(([key,value]) =>
      Array.isArray(value) ? value.includes(liveState[key]) : liveState[key] === value);
  const edges = topology.edges.map(edge => {
    const widget = widgets.find(item => item.type === 'joystick' && item.node_state
      && item.enter_action === edge.transition.action);
    const target = widget && topology.nodes.find(node => node.value === widget.node_state
      && node.region.state_field === (widget.state_field || primary?.state_field));
    return {...edge,dst:target?.id || edge.dst,reason:!edge.transition.action
      ? offline ? '请先连接设备' : stale ? '状态同步中' : ''
      : widget && remoteActive(widget) ? '已进入遥控' : edge.active
      ? stateActionReason(sm,edge.region,edge.transition,liveState,{offline,busy,actor,stale})
      : '当前状态不可执行'};
  });
  const nodes = topology.nodes.map(node => {
    const control = stateNodeControl(node,edges);
    if (!node.current && !control.transition && !control.choices.length) {
      const incoming = edges.find(edge => edge.dst === node.id && edge.transition.action);
      const reason = incoming && stateActionReason(sm,incoming.region,incoming.transition,
        liveState,{offline,busy,actor,stale});
      if (reason) control.reason = reason;
    }
    return {...node,control};
  });
  const selected = nodes.find(node => node.id === selectedId);
  const selectedActions = selected?.control.choices.filter(edge => !edge.reason) || [];
  const links = stateGraphLinks(edges);
  const selectedLink = links.find(link => link.id === selectedLinkId);
  const selectedDirection = selectedLink && stateLinkDirection(selectedLink,nodes,selectedDirectionId);
  const select = async (id, nextAnchor) => {
    const node = nodes.find(item => item.id === id);
    const remote = widgets.find(widget => widget.type === 'joystick' && widget.visible !== false
      && widget.node_state === node?.value
      && (widget.state_field || primary?.state_field) === node?.region.state_field);
    const next = remote || node?.control.transition ? id : id === selectedId ? null : id;
    setSelectedId(next);
    setSelectedLinkId(null); setSelectedDirectionId(null);
    setAnchor(next ? nextAnchor : null);
    onSelectState?.(next && node?.region.id === primary?.id ? node.value : null);
    // An event-driven destination is reachable but is not a command. Inspect
    // its declared rule instead of inventing an action or leaving a dead tap.
    if (next && !remote && !node?.control.choices.some(edge => !edge.reason) && node?.control.automatic.length) {
      const automatic = node.control.automatic[0];
      const link = links.find(item => item.directions.some(direction => direction.edges.includes(automatic)));
      const direction = link?.directions.find(item => item.edges.includes(automatic));
      if (direction) {
        setSelectedLinkId(link.id); setSelectedDirectionId(direction.id);
        const element = [...canvasRef.current.querySelectorAll('.fsm-whiteboard__edge')]
          .find(item => item.dataset.link === link.id);
        const path = element?.querySelector('.fsm-whiteboard__line');
        if (path) setAnchor({element,point:path.getPointAtLength(path.getTotalLength()/2),keyboard:nextAnchor?.keyboard});
      }
    }
    if (nodeCommandPending.current || joystickExiting || busy || offline || stale) return;
    const transition = remote ? !remoteActive(remote) && edges.find(edge => edge.dst === id && edge.active && !edge.reason
      && edge.transition.action === remote.enter_action)?.transition : node?.control.transition;
    if (!transition) return;
    nodeCommandPending.current = true;
    try { await onTransition?.(transition); }
    catch (error) { report?.({result:'error',error:apiErrorText(error)}); }
    finally { nodeCommandPending.current = false; }
  };
  const inspectEdge = (link, direction, nextAnchor) => {
    if (link.id === selectedLinkId) { closePopover(); return; }
    setSelectedLinkId(link.id); setSelectedDirectionId(direction.id);
    setSelectedId(null);
    setAnchor(nextAnchor);
    onSelectState?.(null);
  };
  useEffect(() => { clearSelection(); }, [unitId,liveState.epoch,clearSelection]);
  const ordered = widgets.map((widget,index) => ({...widget,_index:index}))
    .sort((a,b) => (a.order ?? a._index)-(b.order ?? b._index));
  const active = ordered.filter(widget => !widget.states?.length
    || widget.states.includes(liveState[widget.state_field || primary?.state_field] || current));
  const visible = active.filter(widget => widget.visible !== false && widget.type !== 'connection'
    && (!widget.preview_only || selected?.current));
  const media = visible.filter(widget => ['video','map'].includes(widget.type));
  const controls = visible.filter(widget => !media.includes(widget)
    && !(widget.type === 'joystick' && widget.node_state)
    && (!widget.requires_connection || connected));
  const remoteControls = ordered.filter(widget => widget.type === 'joystick' && widget.node_state
    && widget.visible !== false && (remoteActive(widget) || selected?.value === widget.node_state
      && selected.region.state_field === (widget.state_field || primary?.state_field)));
  const renderWidget = widget => window.fsmWidget?.(unitId,
    liveState[widget.state_field || primary?.state_field] || current,
    [{...widget,states:widget.type === 'joystick' && widget.node_state ? [] : widget.states,
      _disabled:offline || stale || (widget.type === 'joystick' && (busy || joystickExiting
        || sm?.freshness_field && liveState[sm.freshness_field] === false))}],report,actor,liveState,
    widget.state_field || primary?.state_field,sm?.connection_field || '',connected);
  const phase = ({sending:'正在发送',dispatched:'已发送',sent:'等待设备确认',accepted:'已接收，完成待确认',executing:'正在执行',
    timeout:'完成反馈超时',cancelled_unconfirmed:'已请求取消，结果待确认',stop_requested_unconfirmed:'已请求停止，结果待确认',
    recovered:'姿态已恢复',completed:'已完成',ok:'已完成',rejected:'设备拒绝请求',failed:'执行失败',error:'执行失败',cancelled:'已取消'})[execution?.phase || execution?.result] || '等待设备反馈';
  const executionLabel = execution?.label || topology.edges.find(edge => [edge.transition.trigger,edge.transition.action]
    .includes(execution?.command))?.transition.label || '设备动作';
  return <div className="device-state-board device-whiteboard"
    style={{'--board-accent':color}} aria-busy={busy}
    onKeyDown={event => { if (event.key === 'Escape' && anchor) { event.stopPropagation(); closePopover(); } }}>
    {media.length > 0 && <div className="device-whiteboard__media" aria-label="实时画面与地图">
      {media.map(widget => <div key={`${widget.type}:${widget._index}`} className="device-whiteboard__media-slot"><div
        className="device-whiteboard__media-tile">
        {connected ? renderWidget(widget) : <span className="device-whiteboard__media-empty">{widget.type === 'video' ? '视频 · 等待连接' : '地图 · 等待连接'}</span>}
      </div></div>)}
    </div>}
    <div ref={canvasRef} className="device-whiteboard__canvas">
      {nodes.length ? <FsmGraph nodes={nodes} edges={edges} selectedState={selectedId} diagram={sm?.diagram}
        selectedLink={selectedLinkId} selectedDirection={selectedDirection?.id} onSelectState={select} onInspectTransition={inspectEdge}
        onDismiss={clearSelection}
        color={color} pendingCommand={pendingExecution?.command}/>
        : <p className="device-state-note">等待设备状态机</p>}
      {selected && (selectedActions.length > 1 || selected.region.until_field && liveState[selected.region.until_field])
        && !remoteControls.some(widget => selected.value === widget.node_state) && <StateBoardPopover
          anchor={anchor} canvasRef={canvasRef} label="状态说明" onClose={closePopover}>
        <strong>{selected.label}</strong><div className="device-whiteboard__popover-body">
          {selectedActions.length > 1 && <div className="device-whiteboard__actions">{selectedActions.map(edge =>
            <button key={edge.id} type="button" className="btn" disabled={!!edge.reason}
              onClick={() => { clearSelection(); onTransition?.(edge.transition); }}>{stateTransitionLabel(edge.transition)}</button>)}</div>}
          {selected.region.until_field && liveState[selected.region.until_field] && selected.current
            && <small>至 {new Date(liveState[selected.region.until_field]*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}</small>}
        </div>
      </StateBoardPopover>}
      {selectedDirection && <StateBoardPopover anchor={anchor} canvasRef={canvasRef} label="转换说明" onClose={closePopover}>
        <div className="device-whiteboard__directions" role="group" aria-label="转换方向">
          {selectedLink.directions.map(direction => <button key={direction.id} type="button"
            aria-pressed={direction.id === selectedDirection.id}
            onClick={() => setSelectedDirectionId(direction.id)}>
            {nodes.find(node => node.id === direction.src)?.label} → {nodes.find(node => node.id === direction.dst)?.label}
          </button>)}
        </div>
        <div className="device-whiteboard__popover-body" key={selectedDirection.id}>
          {selectedDirection.edges.map(edge => <article key={edge.id} className="device-whiteboard__system-rule">
            {edge.transition.action ? <div className="device-whiteboard__actions"><button type="button" className="btn"
              disabled={!!edge.reason} onClick={() => onTransition?.(edge.transition)}>{stateTransitionLabel(edge.transition)}</button></div>
              : <p>{stateTransitionLabel(edge.transition)}</p>}
            {stateTransitionDescription(edge.transition) && <small>{stateTransitionDescription(edge.transition)}</small>}
          </article>)}
          <StateTransitionRules edges={selectedDirection.edges} records={rules} card={card}
            error={canReadRules && ruleSnapshot?.unitId === unitId && ruleSnapshot.error} onRetry={loadRules}
            onEdit={id => { clearSelection(); setEditingRule(id); }}/>
        </div>
      </StateBoardPopover>}
    </div>
    {editingRule && ReactDOM.createPortal(<RulesDrawer open={true} initialTeachingId={editingRule}
      onClose={() => { setEditingRule(null); loadRules(); }}/>,document.body)}
    {(feedback || offline || stale || execution || awaitingFeedback) && <div className="device-whiteboard__feedback" role="status" aria-live="polite">
      {feedback && <div>{feedback}</div>}
      {offline ? <div>设备未连接，暂不能执行动作</div>
        : stale ? <div>状态同步中，普通操作等待同步</div>
        : <>
          {execution && <div>{executionLabel} · {phase}</div>}
          {awaitingFeedback && <div>{primary?.manual_available_actions_field
            ? '姿态暂未确认，手动动作仍可发送'
            : '设备已连接，等待有效设备反馈；当前状态尚未确认，暂不能切换'}</div>}
        </>}
    </div>}
    {(controls.length > 0 || remoteControls.length > 0) && <div className="device-whiteboard__controls">
      {remoteControls.map(widget => {
        const engaged = remoteActive(widget);
        const entry = edges.find(edge => edge.active && edge.transition.action === widget.enter_action);
        const reason = entry?.reason || (!entry ? '当前状态不可进入遥控' : '');
        return <section key={widget._index} className="device-remote-controls" aria-label="移动控制">
          <header><strong>移动</strong><span>{engaged ? '松手停止' : busy ? '正在进入遥控…' : reason ? '' : '等待遥控就绪'}</span>
            {engaged && <button type="button" disabled={offline || !connected || joystickExiting}
              onClick={async () => {
                setJoystickExiting(true);
                try { await onTransition({action:widget.exit_action,params:{},label:'退出遥控',safety:true}); }
                finally { setJoystickExiting(false); }
              }}>退出遥控</button>}
          </header>
          {renderWidget(widget)}
          {!engaged && reason && <small role="status">{reason}</small>}
        </section>;
      })}
      {controls.map(widget => widget.type === 'joystick'
        ? <details key={widget._index} open={stateWidgetEnabled(widget,liveState) && !offline && !stale || undefined}>
          <summary>遥控{stateWidgetEnabled(widget,liveState) ? '' : ' · 当前不可用'}</summary>{renderWidget(widget)}</details>
        : <div key={widget._index}>{renderWidget(widget)}</div>)}
    </div>}
    {active.filter(widget => widget.visible === false).map(widget => <React.Fragment key={widget._index}>{renderWidget(widget)}</React.Fragment>)}
  </div>;
}
const HierarchicalFsmBoard = DeviceStateBoard;
window.DeviceStateBoard = DeviceStateBoard;
window.HierarchicalFsmBoard = HierarchicalFsmBoard;
