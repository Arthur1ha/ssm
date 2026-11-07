/* Shared flat whiteboard. Device Card remains the authority for all commands. */
function DeviceStateBoard({ unitId, sm, current, widgets = [], liveState = {}, connected,
  offline, actor, color, busy, onSelectState, onTransition, report, streamHealth, feedback }) {
  const { useState, useRef, useEffect } = React;
  const [selectedId, setSelectedId] = useState(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState(null);
  const [expanded, setExpanded] = useState(false);
  const [joystickExiting, setJoystickExiting] = useState(false);
  const boardRef = useRef(null);
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
    return {...edge,dst:target?.id || edge.dst,reason:widget && remoteActive(widget) ? '已进入遥控' : edge.active
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
  const selectedEdge = edges.find(edge => edge.id === selectedEdgeId);
  const select = id => {
    const node = topology.nodes.find(item => item.id === id);
    const next = id === selectedId ? null : id;
    setSelectedId(next);
    setSelectedEdgeId(null);
    onSelectState?.(next && node?.region.id === primary?.id ? node.value : null);
  };
  const activateState = id => {
    const node = nodes.find(item => item.id === id);
    if (!node) return;
    select(id);
    if (node.control.transition) onTransition?.(node.control.transition);
  };
  const inspectEdge = edge => {
    setSelectedEdgeId(edge.id === selectedEdgeId ? null : edge.id);
    setSelectedId(null);
    onSelectState?.(null);
  };
  useEffect(() => { setSelectedId(null); setSelectedEdgeId(null); setExpanded(false); }, [unitId,liveState.epoch]);
  useEffect(() => {
    if (!expanded) return;
    const restoreFocus = document.activeElement;
    const onKey = event => {
      // Media dialogs live outside the board and own their keyboard handling.
      if (!boardRef.current?.contains(event.target)) return;
      if (event.key === 'Escape') {
        event.stopPropagation();
        setExpanded(false);
      }
      if (event.key === 'Tab') {
        const root = boardRef.current;
        const controls = [...(root?.querySelectorAll('button:not(:disabled),[tabindex="0"]') || [])];
        const first = controls[0], last = controls[controls.length-1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    };
    document.addEventListener('keydown',onKey);
    return () => { document.removeEventListener('keydown',onKey); restoreFocus?.focus?.(); };
  }, [expanded]);
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
  return <div ref={boardRef} className={`device-state-board device-whiteboard${expanded ? ' is-expanded' : ''}`}
    style={{'--board-accent':color}} aria-busy={busy} role={expanded ? 'dialog' : undefined}
    aria-modal={expanded || undefined} aria-label={expanded ? '设备白板' : undefined}>
    {media.length > 0 && <div className="device-whiteboard__media" aria-label="实时画面与地图">
      {media.map(widget => <div key={`${widget.type}:${widget._index}`} className="device-whiteboard__media-slot"><div
        className="device-whiteboard__media-tile">
        {connected ? renderWidget(widget) : <span className="device-whiteboard__media-empty">{widget.type === 'video' ? '视频 · 等待连接' : '地图 · 等待连接'}</span>}
      </div></div>)}
    </div>}
    <div className="device-whiteboard__canvas">
      {nodes.length ? <FsmGraph nodes={nodes} edges={edges} selectedState={selectedId} diagram={sm?.diagram}
        onSelectState={activateState} onInspectTransition={inspectEdge}
        color={color} pendingCommand={pendingExecution?.command}/>
        : <p className="device-state-note">等待设备状态机</p>}
      <button type="button" className="device-whiteboard__expand" aria-label={expanded ? '退出白板全屏' : '白板全屏'}
        onClick={() => setExpanded(value => !value)}>{expanded ? '收起' : '全屏'}</button>
      {selected && !remoteControls.some(widget => selected.value === widget.node_state) && <section className="device-whiteboard__preview" aria-label="状态说明">
        <StateVisual state={selected.value} view={selected.view} color={color}/>
        <div><strong>{selected.label}{selected.current ? ' · 当前状态' : ''}</strong>
          <p>{selected.control.reason || selected.view?.description || '点击目标状态，执行当前允许的转换。'}</p>
          {!selected.current && selected.control.choices.length > 1 && selected.control.choices.map(edge =>
            <button key={edge.id} type="button" disabled={!!edge.reason} title={edge.reason || ''}
              onClick={() => onTransition?.(edge.transition)}>{edge.transition.label}</button>)}
          {selected.current && selected.control.choices.filter(edge => edge.transition.available_only || edge.transition.dispatch_only).map(edge =>
            <button key={`current:${edge.id}`} type="button" disabled={!!edge.reason} title={edge.reason || ''}
              onClick={() => onTransition?.(edge.transition)}>{edge.transition.label}</button>)}
          {selected.region.until_field && liveState[selected.region.until_field] && selected.current
            && <small>至 {new Date(liveState[selected.region.until_field]*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}</small>}
        </div>
        <button type="button" aria-label="关闭状态说明" onClick={() => select(selected.id)}>×</button>
      </section>}
      {selectedEdge && <section className="device-whiteboard__preview" aria-label="转换说明">
        <div><strong>{nodes.find(node => node.id === selectedEdge.src)?.label} → {nodes.find(node => node.id === selectedEdge.dst)?.label}</strong>
          <p>{selectedEdge.transition.label}{selectedEdge.transition.description ? `：${selectedEdge.transition.description}` : ''}</p>
          <small>{selectedEdge.transition.action ? selectedEdge.reason || '当前可转换，点击目标状态执行。' : '根据设备反馈或任务结果自动转换。'}</small>
          {selectedEdge.transition.action && <button type="button" className="btn"
            disabled={!!selectedEdge.reason} onClick={() => onTransition?.(selectedEdge.transition)}>
            {selectedEdge.transition.label}
          </button>}
        </div>
        <button type="button" aria-label="关闭转换说明" onClick={() => setSelectedEdgeId(null)}>×</button>
      </section>}
    </div>
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
          <header><strong>移动</strong><span>{engaged ? '松手停止' : '先进入遥控，再使用摇杆'}</span>
            {engaged ? <button type="button" disabled={offline || !connected || joystickExiting}
              onClick={async () => {
                setJoystickExiting(true);
                try { await onTransition({action:widget.exit_action,params:{},label:'退出遥控',safety:true}); }
                finally { setJoystickExiting(false); }
              }}>退出遥控</button>
              : <button type="button" disabled={!!reason || busy || offline || stale} title={reason}
                onClick={() => onTransition(entry.transition)}>进入遥控</button>}
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
