/* FsmDevicePage — 通用状态机设备页。
 * 固定控制区展示 Card 声明的状态与转移；当前态内容和对话排在其后。
 * 核心控制由 Agent Card 的 transport / state_machine 驱动。
 */
function FsmDevicePage({ unitId, device, liveState, onBack, actor }) {
  const { useState, useEffect, useRef } = React;

  /* ── Card 拓扑：优先用 device prop（来自 registry，无需等待 fetch） ── */
  const [sm, setSm]                 = useState(device?.state_machine || null);
  const [transport, setTransport]   = useState(device?.transport || null);
  const [cardLoaded, setCardLoaded] = useState(!!(device?.state_machine || device?.transport));

  /* ── HTTP 设备实时状态 ── */
  const [sseState, setSseState] = useState(null);
  const esRef = useRef(null);

  /* ── 对话 ── */
  const initialMsg = {
    role: 'assistant', agent: unitId,
    text: `你好，我是 ${device?.name || unitId}，有什么可以帮你？`,
  };
  const [messages, setMessages] = useState([initialMsg]);
  const [sending, setSending] = useState(false);
  const [execution, setExecution] = useState('');
  const [go2AssetsOpen, setGo2AssetsOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const reportExecution = result => setExecution(commandBus.describe(result));

  // 命令反馈是短暂提示；设备的当前状态由下方实时状态图呈现。
  useEffect(() => {
    if (!execution || execution.startsWith('正在')) return;
    const timer = setTimeout(() => setExecution(''), 4500);
    return () => clearTimeout(timer);
  }, [execution]);

  /* ── 模式轴 / 富控件 / 快捷指令（通用，由 card 驱动） ── */
  const [modes, setModes]       = useState(device?.modes || []);
  const [modeVals, setModeVals] = useState({});   // axis.id → 当前 value
  const [widgets, setWidgets]   = useState(device?.widgets || []);
  const [suggestions, setSuggestions] = useState(device?.suggestions || []);

  // manifest/card 热更新后同步拓扑与控件（fetch 见下方 Agent Card effect）
  useEffect(() => { if (device?.state_machine) setSm(device.state_machine); }, [device?.state_machine]);
  useEffect(() => { if (device?.transport) setTransport(device.transport); }, [device?.transport]);
  useEffect(() => { if (device?.modes) setModes(device.modes); }, [device?.modes]);
  useEffect(() => { if (device?.widgets) setWidgets(device.widgets); }, [device?.widgets]);
  useEffect(() => { if (device?.suggestions) setSuggestions(device.suggestions); }, [device?.suggestions]);

  // 各轴拉当前值（http: GET 端点返回 {mode}）
  // 用轴 id 拼接字符串作依赖，避免 setModes 产生新数组引用时重复 fetch
  const modeIds = modes.map(a => a.id).join(',');
  useEffect(() => {
    modes.forEach(axis => {
      if (!axis.get) return;
      fetch(axis.get)
        .then(r => r.json())
        .then(d => d.mode && setModeVals(prev => ({ ...prev, [axis.id]: d.mode })))
        .catch(() => {});
    });
  }, [modeIds]);

  const switchMode = (axis, value) => {
    if (offline) return;
    if (!axis.set) return;
    commandBus.command(unitId, 'set_mode', { axis: axis.id, value }).then(result => {
      if (result.mode === value) {
        setModeVals(prev => ({ ...prev, [axis.id]: value }));
        setExecution('模式已更新');
      } else reportExecution(result);
    });
  };

  /* ── 后台刷新 Agent Card（prop 已提供初始值，fetch 仅作热更新） ── */
  useEffect(() => {
    fetch('/api/devices/' + unitId + '/agent')
      .then(r => r.json())
      .then(c => {
        if (c.state_machine) setSm(c.state_machine);
        if (c.transport)     setTransport(c.transport);
        if (c.modes)         setModes(c.modes);
        if (c.widgets)       setWidgets(c.widgets);
        if (c.suggestions)   setSuggestions(c.suggestions);
        setCardLoaded(true);
      })
      .catch(() => setCardLoaded(true));
  }, [unitId]);

  /* ── HTTP 设备：订阅 SSE 实时状态流 ── */
  useEffect(() => {
    const url = transport?.state_stream;
    if (!url) return;
    const es = new EventSource(url);
    esRef.current = es;
    es.onmessage = e => {
      try {
        const d = JSON.parse(e.data);
        if (d.fsm_state) setSseState(d.fsm_state);
      } catch (_) {}
    };
    return () => { es.close(); esRef.current = null; };
  }, [transport?.state_stream]);

  /* ── 当前态 ── */
  const isHttp  = transport?.kind === 'http';
  const rawCurrent = isHttp
    ? (sseState || sm?.initial || '')
    : (liveState?.state?.ism || device?.state?.ism || sm?.initial || '');
  const isLed = agentKind(device || { unit_id: unitId, name: unitId }) === 'led';
  const ledState = liveState?.state || device?.state || {};
  const ledInfo = isLed ? getLedStateInfo(ledState) : null;
  // 兼容旧固件 retained 状态与新版三状态卡片的短暂交错。
  const legacyLedStates = { BRIGHT: 'ON', DIM: 'ON', COLOR: 'ON', BLINK: 'BLINKING' };
  const current = isLed && sm?.states?.includes('ON') && !sm.states.includes(rawCurrent)
    ? (legacyLedStates[rawCurrent] || rawCurrent)
    : rawCurrent;

  /* ── 派发 FSM 转移 ── */
  const fire = async trigger => {
    if (offline) return;
    const t = (sm?.transitions || []).find(
      tr => tr.src === current && tr.trigger === trigger
    );
    if (!t?.action) {
      setExecution(t?.hint || '当前状态不允许这个动作');
      return;
    }
    setExecution('正在等待设备执行反馈…');
    const result = await commandBus.command(unitId, t.action, t.params || {});
    if (t.safety && result?.ok) {
      setExecution(`${t.label}已发送，等待状态同步`);
    } else if (result?.result === 'accepted' && result.fsm_state === t.dst) {
      setExecution('');
    } else {
      reportExecution(result);
    }
  };

  /* ── 发送对话 ── */
  const handleSend = text => {
    if (!text || sending) return;
    setMessages(prev => [...prev, { role: 'user', text }]);
    setSending(true);
    commandBus.command(unitId, 'chat', { message: text })
      .then(result => {
        const reply = result?.reply || result?.response || result?.text || result?.error || commandBus.describe(result);
        setMessages(prev => [...prev, { role: 'assistant', agent: unitId, text: reply }]);
      })
      .finally(() => setSending(false));
  };

  const meta    = getAgentMeta(device || { unit_id: unitId, name: unitId });
  const ACCENT  = meta.color;
  const offline = !device?._online;   // 离线：控制面板置灰、禁用交互
  const isGo2 = unitId === 'go2' || device?.device_id === 'go2';
  const isOn    = current && current !== 'OFF' && current !== 'IDLE';  // 设备活跃态（发光）
  const hasUserMsg = messages.some(m => m.role === 'user');
  const hasBoardWidget = widgets.some(w => w.visible !== false
    && (!w.states?.length || w.states.includes(current)));
  const statusLabel = offline ? '离线' : `在线 · ${isLed && ledState.ism ? ledInfo.label : (sm?.views?.[current]?.label || current || '状态同步中')}`;

  return (
    <div style={{
      position: 'fixed', inset: 0,
      background: 'var(--color-bg)', color: '#ccc',
      fontFamily: 'var(--font-sans)',
      paddingTop: 'env(safe-area-inset-top,0px)',
      display: 'flex', flexDirection: 'column',
    }}>

      {/* ── 头部 ── */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '7px 16px',
        borderBottom: '1px solid var(--color-border)',
        background: 'var(--color-surface-1)',
        flexShrink: 0,
      }}>
        {/* 返回按钮 */}
        <button onClick={onBack} aria-label="返回空间" style={{
          background: 'none', border: 'none',
          color: 'var(--color-text-dim)', cursor: 'pointer',
          fontSize: 18, padding: '0 4px', lineHeight: 1,
          fontFamily: 'inherit', WebkitTapHighlightColor: 'transparent',
        }}>←</button>

        {/* 设备名与右侧实时状态 */}
        <div style={{ minWidth: 0, flex: '0 1 auto' }}>
          <div style={{
            fontSize: 15, fontWeight: 700, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            color: offline ? 'var(--color-text-dim)' : ACCENT,
            textShadow: (isOn && !offline) ? `0 0 12px ${ACCENT}80` : 'none',
            transition: 'text-shadow 0.4s',
          }}>{device?.name || unitId}</div>
        </div>
        <div className="device-header__status" title={statusLabel}>
          <i className={offline ? 'is-offline' : ''}/>
          <span>{statusLabel}</span>
        </div>
        <button type="button" className="device-header__settings"
          onClick={() => setSettingsOpen(true)} aria-label="设备设置">⋯</button>
      </div>

      {execution && <div role="status" aria-live="polite" style={{ padding: '8px 16px', fontSize: 12 }}>{execution}</div>}
      {/* ── Card 驱动的设备白板。 ── */}
      {cardLoaded && (
        <div className={`device-state-workspace${hasBoardWidget ? ' device-state-workspace--expanded' : ''}`} style={{
          filter: offline ? 'grayscale(1)' : 'none',
          opacity: offline ? 0.45 : 1,
          pointerEvents: offline ? 'none' : 'auto',
          transition: 'filter 0.2s, opacity 0.2s',
        }}>
          <FsmBoard unitId={unitId} sm={sm} current={current} widgets={widgets}
            onFire={fire} report={reportExecution} actor={actor} color={ACCENT}
            liveState={ledState}
            legacyLed={isLed && sm && !sm.states?.includes('ON')}/>
        </div>
      )}

      {/* ── ChatPanel（撑满剩余高度）；离线整体置灰并禁用输入 ── */}
      <div style={{
        flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column',
        filter: offline ? 'grayscale(1)' : 'none',
        opacity: offline ? 0.45 : 1,
        transition: 'filter 0.2s, opacity 0.2s',
      }}>
      <div className="device-chat-heading">与{device?.name || unitId}对话</div>
      <ChatPanel
        messages={messages}
        thinking={sending}
        thinkingText={sending ? '正在回应…' : ''}
        thinkingAgent={unitId}
        onSend={handleSend}
        placeholder={offline ? '设备离线，暂不可用' : '告诉设备要做什么…'}
        disabled={offline}
        variant="inline"
      >
        {/* 快捷指令（card.suggestions 驱动）：无用户消息时显示，位置与主屏建议一致 */}
        {suggestions.length > 0 && !hasUserMsg && !sending && !offline && (
          <div style={{
            padding: '6px 12px 0', display: 'flex', gap: 6,
            overflowX: 'auto', scrollbarWidth: 'none',
          }}>
            {suggestions.map(cmd => (
              <button key={cmd} onClick={() => handleSend(cmd)} style={{
                flexShrink: 0, padding: '7px 14px',
                borderRadius: 'var(--radius-card)',
                background: 'var(--color-surface-2)',
                border: '1px solid var(--color-border-strong)',
                color: 'var(--color-text-muted)', fontSize: 13,
                cursor: 'pointer', fontFamily: 'inherit', whiteSpace: 'nowrap',
                WebkitTapHighlightColor: 'transparent',
              }}>{cmd}</button>
            ))}
          </div>
        )}
      </ChatPanel>
      </div>
      <DeviceSettingsMenu open={settingsOpen} onClose={() => setSettingsOpen(false)}
        modes={modes} modeVals={modeVals} onSwitchMode={switchMode}
        offline={offline} isGo2={isGo2} actor={actor} unitId={unitId}
        onGo2Settings={() => setGo2AssetsOpen(true)}/>
      {actor === 'admin' && isGo2 && <Go2AssetsDrawer open={go2AssetsOpen} onClose={() => setGo2AssetsOpen(false)}/>}
    </div>
  );
}
window.FsmDevicePage = FsmDevicePage;

function FsmBoard({ unitId, sm, current, widgets, onFire, report, actor, color, liveState, legacyLed }) {
  const { useState, useRef, useEffect } = React;
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [zoom, setZoom] = useState(1);
  const drag = useRef(null);
  const pointers = useRef(new Map());
  const pinch = useRef(null);
  const activeWidgets = widgets.map((widget, index) => ({ ...widget, _index: index }))
    .filter(widget => !widget.states?.length || widget.states.includes(current));
  const visibleWidgets = activeWidgets.filter(widget => widget.visible !== false)
    .sort((a, b) => (a.order ?? a._index) - (b.order ?? b._index));
  const placement = widget => widget.placement === 'media' || (!widget.placement && widget.type === 'video')
    ? 'media' : 'controls';
  const mediaWidgets = visibleWidgets.filter(widget => placement(widget) === 'media');
  const controlWidgets = visibleWidgets.filter(widget => placement(widget) === 'controls');
  const hiddenWidgets = activeWidgets.filter(widget => widget.visible === false);
  const view = sm?.views?.[current];
  const showPreview = view?.type === 'robot_pose' && !mediaWidgets.length;
  const renderWidget = widget => <div key={`${widget.type}-${widget._index}`}
    className={`device-board__widget device-board__widget--${placement(widget)}`}>
    {window.fsmWidget?.(unitId, current, [widget], report, actor, liveState)}
  </div>;
  const safety = (sm?.transitions || []).find(t => t.src === current && t.safety && t.action);

  // A new state starts with the graph centered; widgets stay in document flow below it.
  useEffect(() => {
    drag.current = null;
    pointers.current.clear();
    pinch.current = null;
    setPan({ x: 0, y: 0 });
    setZoom(1);
  }, [current]);

  const startPan = event => {
    if (event.button !== 0 || event.target.closest('[role="button"], button')) return;
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, pan };
    event.currentTarget.setPointerCapture(event.pointerId);
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      pinch.current = { distance: Math.hypot(a.x - b.x, a.y - b.y), zoom };
      drag.current = null;
    }
  };
  const movePan = event => {
    if (pointers.current.has(event.pointerId)) {
      pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    }
    if (pinch.current && pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      const distance = Math.hypot(a.x - b.x, a.y - b.y);
      setZoom(Math.max(0.85, Math.min(1.2, pinch.current.zoom * distance / Math.max(1, pinch.current.distance))));
      return;
    }
    if (drag.current?.id !== event.pointerId) return;
    setPan({
      x: Math.max(-180, Math.min(180, drag.current.pan.x + event.clientX - drag.current.x)),
      y: Math.max(-150, Math.min(150, drag.current.pan.y + event.clientY - drag.current.y)),
    });
  };
  const endPan = event => {
    pointers.current.delete(event.pointerId);
    if (pointers.current.size < 2) pinch.current = null;
    if (drag.current?.id === event.pointerId) drag.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };
  return <div className="device-board" style={{ '--board-accent': color }}>
    <div className="device-board__viewport"
      onPointerDown={startPan} onPointerMove={movePan} onPointerUp={endPan} onPointerCancel={endPan}>
      <div className="device-board__canvas" style={{ transform: `translate(calc(-50% + ${pan.x}px), calc(-50% + ${pan.y}px)) scale(${zoom})` }}>
        {sm ? <FsmGraph states={sm.states} transitions={sm.transitions} current={current}
          onFire={onFire} color={color} views={sm.views || {}} layout={sm.layout || {}}
          showSafety={false}/>
          : <div className="device-state-workspace__fallback">{current || '—'}</div>}
      </div>
    </div>
    {hiddenWidgets.map(widget => <React.Fragment key={`hidden-${widget._index}`}>
      {window.fsmWidget?.(unitId, current, [widget], report, actor, liveState)}
    </React.Fragment>)}
    {(mediaWidgets.length > 0 || controlWidgets.length > 0 || showPreview) && <div className="device-board__widget-tray">
      {mediaWidgets.map(renderWidget)}
      {showPreview && <div className="device-board__widget device-board__widget--media device-board__widget--preview">
        <StateVisual view={view} state={current} color={color}/>
      </div>}
      {controlWidgets.map(renderWidget)}
    </div>}
    {(legacyLed || safety) && <div className="device-board__footer">
      {legacyLed && <span className="device-board__legacy">旧版状态机 · 固件待更新</span>}
      {safety && <button type="button" className="fsm-safety-edge device-board__safety"
        onClick={() => onFire(safety.trigger)} aria-label={`${safety.label}，${sm?.views?.[current]?.label || current}到${sm?.views?.[safety.dst]?.label || safety.dst}`}>
        <span>{sm?.views?.[current]?.label || current}</span><b>── {safety.label} ──▶</b><span>{sm?.views?.[safety.dst]?.label || safety.dst}</span>
      </button>}
    </div>}
  </div>;
}

function DeviceSettingsMenu({ open, onClose, modes, modeVals, onSwitchMode, offline, isGo2, actor, unitId, onGo2Settings }) {
  const panelRef = React.useRef(null);
  const closeRef = React.useRef(onClose);
  closeRef.current = onClose;
  React.useEffect(() => {
    if (!open) return undefined;
    panelRef.current?.querySelector('button')?.focus();
    const onKey = event => {
      if (event.key === 'Escape') closeRef.current();
      if (event.key !== 'Tab') return;
      const focusables = panelRef.current?.querySelectorAll('button:not([disabled]), a[href]');
      if (!focusables?.length) return;
      if (event.shiftKey && document.activeElement === focusables[0]) {
        event.preventDefault();
        focusables[focusables.length - 1].focus();
      } else if (!event.shiftKey && document.activeElement === focusables[focusables.length - 1]) {
        event.preventDefault();
        focusables[0].focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);
  if (!open) return null;

  return <div className="device-settings-layer">
    <div className="device-settings-scrim" onClick={onClose}/>
    <section ref={panelRef} className="device-settings-panel" role="dialog" aria-modal="true" aria-label="设备设置">
      <header><h2>设备设置</h2><button type="button" className="btn" onClick={onClose} aria-label="关闭设备设置"><Icon name="x" size={15}/></button></header>
      {actor === 'admin' && modes.map(axis => <section key={axis.id} className="device-settings-axis">
        <h3>{axis.label}</h3>
        <div>{axis.options.map(option => <button type="button" key={option.value}
          className={modeVals[axis.id] === option.value ? 'is-active' : ''}
          onClick={() => onSwitchMode(axis, option.value)} disabled={offline}
          title={option.description}>
          <strong>{option.label}</strong><small>{option.description}</small>
        </button>)}</div>
      </section>)}
      {actor === 'admin' && isGo2 && <button type="button" className="device-settings-go2" onClick={() => { onClose(); onGo2Settings(); }}>
        <span><strong>Go2 设置</strong><small>地点、人设与记忆</small></span><Icon name="arrow" size={15}/>
      </button>}
      <a className="device-settings-card" href={'/api/devices/' + encodeURIComponent(unitId) + '/agent'}
        target="_blank" rel="noopener noreferrer">
        <span><strong>Agent Card</strong><small>查看设备能力、状态机和控件声明</small></span><Icon name="arrow" size={15}/>
      </a>
    </section>
  </div>;
}
