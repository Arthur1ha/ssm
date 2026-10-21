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

  // card 热更新后同步 modes/widgets/suggestions（fetch 见下方 Agent Card effect）
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
  const current = isHttp
    ? (sseState || sm?.initial || '')
    : (liveState?.state?.ism || sm?.initial || '');

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
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '11px 16px',
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

        {/* 设备名 + 实时状态 */}
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{
            fontSize: 15, fontWeight: 700, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            color: offline ? 'var(--color-text-dim)' : ACCENT,
            textShadow: (isOn && !offline) ? `0 0 12px ${ACCENT}80` : 'none',
            transition: 'text-shadow 0.4s',
          }}>{device?.name || unitId}</div>
          <div className="device-header__status">
            <i className={offline ? 'is-offline' : ''}/>
            {offline ? '离线' : `在线 · ${sm?.views?.[current]?.label || current || '状态同步中'}`}
          </div>
        </div>
        {actor === 'admin' && (modes.length > 0 || isGo2) && <button type="button" className="device-header__settings"
          onClick={() => setSettingsOpen(true)} aria-label="设备设置">⋯</button>}
      </div>

      {execution && <div role="status" aria-live="polite" style={{ padding: '8px 16px', fontSize: 12 }}>{execution}</div>}
      {/* ── Card 驱动的固定状态机控制区 + 当前态内容。 ── */}
      {cardLoaded && (
        <div className="device-state-workspace" style={{
          filter: offline ? 'grayscale(1)' : 'none',
          opacity: offline ? 0.45 : 1,
          pointerEvents: offline ? 'none' : 'auto',
          transition: 'filter 0.2s, opacity 0.2s',
        }}>
          <div className="device-state-workspace__graph">
            <div className="device-state-workspace__heading">状态 · 点击连线切换</div>
            {sm ? (
              <FsmGraph
                states={sm.states}
                transitions={sm.transitions}
                current={current}
                onFire={fire}
                color={ACCENT}
                views={sm.views || {}}
              />
            ) : <div className="device-state-workspace__fallback">{current || '—'}</div>}
          </div>
          <div className="device-state-workspace__content">
            <StateVisual view={sm?.views?.[current]} state={current} color={ACCENT}/>
            {window.fsmWidget && window.fsmWidget(unitId, current, widgets, reportExecution, actor)}
          </div>
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
      {actor === 'admin' && <DeviceSettingsMenu open={settingsOpen} onClose={() => setSettingsOpen(false)}
        modes={modes} modeVals={modeVals} onSwitchMode={switchMode}
        offline={offline} isGo2={isGo2} onGo2Settings={() => setGo2AssetsOpen(true)}/>}
      {actor === 'admin' && isGo2 && <Go2AssetsDrawer open={go2AssetsOpen} onClose={() => setGo2AssetsOpen(false)}/>}
    </div>
  );
}
window.FsmDevicePage = FsmDevicePage;

function DeviceSettingsMenu({ open, onClose, modes, modeVals, onSwitchMode, offline, isGo2, onGo2Settings }) {
  const panelRef = React.useRef(null);
  const closeRef = React.useRef(onClose);
  closeRef.current = onClose;
  React.useEffect(() => {
    if (!open) return undefined;
    panelRef.current?.querySelector('button')?.focus();
    const onKey = event => {
      if (event.key === 'Escape') closeRef.current();
      if (event.key !== 'Tab') return;
      const buttons = panelRef.current?.querySelectorAll('button:not([disabled])');
      if (!buttons?.length) return;
      if (event.shiftKey && document.activeElement === buttons[0]) {
        event.preventDefault();
        buttons[buttons.length - 1].focus();
      } else if (!event.shiftKey && document.activeElement === buttons[buttons.length - 1]) {
        event.preventDefault();
        buttons[0].focus();
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
      {modes.map(axis => <section key={axis.id} className="device-settings-axis">
        <h3>{axis.label}</h3>
        <div>{axis.options.map(option => <button type="button" key={option.value}
          className={modeVals[axis.id] === option.value ? 'is-active' : ''}
          onClick={() => onSwitchMode(axis, option.value)} disabled={offline}
          title={option.description}>
          <strong>{option.label}</strong><small>{option.description}</small>
        </button>)}</div>
      </section>)}
      {isGo2 && <button type="button" className="device-settings-go2" onClick={() => { onClose(); onGo2Settings(); }}>
        <span><strong>Go2 设置</strong><small>地点、人设、人员与记忆</small></span><Icon name="arrow" size={15}/>
      </button>}
    </section>
  </div>;
}
