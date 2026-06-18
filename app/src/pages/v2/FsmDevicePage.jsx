/* FsmDevicePage — 通用状态机设备页（V2）。
 * 布局：头部（固定）→ 状态图（固定高）→ ChatPanel（撑满剩余）
 * 完全由 Agent Card 的 transport / state_machine 驱动，无设备特判。
 */
function FsmDevicePage({ unitId, device, liveState, onBack }) {
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
  const { thinking, thinkingText, send } = useSendIntent();

  /* ── 模式轴（通用，由 card.modes 驱动） ── */
  const [modes, setModes]       = useState(device?.modes || []);
  const [modeVals, setModeVals] = useState({});   // axis.id → 当前 value
  const [widgets, setWidgets]   = useState(device?.widgets || []);

  // card 热更新后同步 modes/widgets（fetch 见下方 Agent Card effect）
  useEffect(() => { if (device?.modes) setModes(device.modes); }, [device?.modes]);
  useEffect(() => { if (device?.widgets) setWidgets(device.widgets); }, [device?.widgets]);

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
    setModeVals(prev => ({ ...prev, [axis.id]: value }));
    if (!axis.set) return;
    fetch(axis.set, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: value }),
    }).catch(() => {});
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
  const fire = trigger => {
    if (offline) return;
    const t = (sm?.transitions || []).find(
      tr => tr.src === current && tr.trigger === trigger
    );
    if (!t?.action) {
      console.warn('[V2] transition missing action:', { unitId, trigger, t });
      return;
    }
    if (isHttp) {
      const endpoint = transport.command_endpoint || transport.endpoint;
      fetch(endpoint, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: t.action, params: t.params || {} }),
      }).catch(e => console.error('[V2] HTTP command failed:', e));
    } else {
      const task_id = 'v2_' + Date.now();
      mqttBus.publish(
        `ssm/task/${unitId}/${task_id}`,
        { task_id, session_id: task_id, action: t.action, params: t.params || {}, ts: Date.now() },
      );
    }
  };

  /* ── 对话直达执行体：优先取 chat_endpoint（灯）或 endpoint（go2） ── */
  const chatEndpoint = transport?.chat_endpoint || transport?.endpoint || null;
  const sessionRef = useRef('fsm_' + unitId + '_' + Date.now());

  /* ── 发送对话 ── */
  const handleSend = text => {
    if (!text || thinking || sending) return;
    setMessages(prev => [...prev, { role: 'user', text }]);

    // 有 chat 端点 → 直达执行体（两类执行体的请求/响应字段不同）
    if (chatEndpoint) {
      const isEsp32 = !!transport?.chat_endpoint;        // 灯：{text}->{reply}
      const body = isEsp32
        ? { text }
        : { session_id: sessionRef.current, message: text };  // go2：{message}->{response}
      setSending(true);
      fetch(chatEndpoint, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
        .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(d => setMessages(prev => [...prev, {
          role: 'assistant', agent: unitId, text: d.reply || d.response || '(无回复)',
        }]))
        .catch(() => setMessages(prev => [...prev, {
          role: 'assistant', agent: unitId, text: '设备没有响应~',
        }]))
        .finally(() => setSending(false));
      return;
    }

    // 否则回退编排器（保持原有逻辑）
    send(text, {
      deviceHint: unitId,
      onMessage: msg => setMessages(prev => [...prev, { role: 'assistant', agent: unitId, text: msg }]),
      onPendingRule: rule => setMessages(prev => [...prev, {
        role: 'assistant', agent: unitId,
        text: `收到规则「${rule.name}」，请在主界面确认保存。`,
      }]),
    });
  };

  const meta    = getAgentMeta(device || { unit_id: unitId, name: unitId });
  const ACCENT  = meta.color;
  const offline = !device?._online;   // 离线：控制面板置灰、禁用交互

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
        <button onClick={onBack} style={{
          background: 'none', border: 'none',
          color: 'var(--color-text-dim)', cursor: 'pointer', fontSize: 18,
        }}>←</button>

        <div style={{
          fontSize: 13, fontWeight: 700, letterSpacing: '0.15em',
          color: offline ? 'var(--color-text-dim)' : ACCENT,
          textShadow: offline ? 'none' : '0 0 14px var(--color-accent)',
        }}>
          {(device?.name || unitId).toUpperCase()}
        </div>

        <div style={{
          marginLeft: 'auto',
          padding: '3px 10px', borderRadius: 999,
          background: offline ? 'var(--color-surface-2)' : 'var(--color-accent-dim)',
          border: `1px solid ${offline ? 'var(--color-border)' : 'rgba(200,255,62,0.3)'}`,
          fontSize: 9, color: offline ? 'var(--color-text-dim)' : ACCENT,
          letterSpacing: '0.12em', fontFamily: 'var(--font-mono)',
        }}>
          {offline ? '离线' : (current || '—').toUpperCase()}
        </div>
      </div>

      {/* ── 状态图区（固定，不随对话滚动）；离线置灰并禁用交互 ── */}
      {cardLoaded && (
        <div style={{
          flexShrink: 0, overflowX: 'hidden',
          filter: offline ? 'grayscale(1)' : 'none',
          opacity: offline ? 0.45 : 1,
          pointerEvents: offline ? 'none' : 'auto',
          transition: 'filter 0.2s, opacity 0.2s',
        }}>
          {sm ? (
            <div style={{
              borderBottom: '1px solid var(--color-border)',
              background: 'var(--color-surface-1)',
              padding: '6px 4px 4px',
            }}>
              <div style={{
                fontSize: 7, color: 'var(--color-text-dim)',
                letterSpacing: '0.22em', textAlign: 'center', marginBottom: 2,
              }}>STATE GRAPH</div>
              <FsmGraph
                states={sm.states}
                transitions={sm.transitions}
                current={current}
                onFire={fire}
                color={ACCENT}
              />
            </div>
          ) : (
            <div style={{
              textAlign: 'center', padding: '16px 0',
              borderBottom: '1px solid var(--color-border)',
              background: 'var(--color-accent-dim)',
            }}>
              <div style={{ fontSize: 8, color: 'var(--color-text-dim)', letterSpacing: '0.25em' }}>
                CURRENT STATE
              </div>
              <div style={{
                fontSize: 24, fontWeight: 700, color: ACCENT, marginTop: 4,
                textShadow: '0 0 18px var(--color-accent)', fontFamily: 'var(--font-mono)',
              }}>
                {(current || '—').toUpperCase()}
              </div>
            </div>
          )}

          {/* 态内富控件（由 card.widgets 驱动） */}
          {window.fsmWidget && window.fsmWidget(unitId, current, widgets)}
        </div>
      )}

      {/* ── ChatPanel（撑满剩余高度）；离线整体置灰并禁用输入 ── */}
      <div style={{
        flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column',
        filter: offline ? 'grayscale(1)' : 'none',
        opacity: offline ? 0.45 : 1,
        transition: 'filter 0.2s, opacity 0.2s',
      }}>
      <ChatPanel
        messages={messages}
        thinking={thinking || sending}
        thinkingText={thinkingText}
        thinkingAgent={unitId}
        onSend={handleSend}
        placeholder={offline ? '设备离线，暂不可用' : '告诉设备要做什么…'}
        disabled={offline}
        variant="inline"
      >
        {/* 模式轴切换（通用，零设备特判） */}
        {modes.map(axis => (
          <div key={axis.id} style={{
            display: 'flex', gap: 6, padding: '8px 12px 0',
            pointerEvents: offline ? 'none' : 'auto',
          }}>
            {axis.options.map((opt, i) => {
              const active = modeVals[axis.id] === opt.value;
              const c = i === 0 ? '#00d4ff' : ACCENT;
              return (
                <button key={opt.value} onClick={() => switchMode(axis, opt.value)} title={opt.description} style={{
                  flex: 1, padding: '7px 4px',
                  background: active ? `${c}18` : 'var(--color-surface-1)',
                  color: active ? c : 'var(--color-text-dim)',
                  border: `1px solid ${active ? `${c}40` : 'var(--color-border)'}`,
                  borderRadius: 'var(--radius-sm)', fontSize: 11, fontWeight: 700,
                  cursor: 'pointer', letterSpacing: '0.07em', fontFamily: 'inherit',
                  WebkitTapHighlightColor: 'transparent', transition: 'all 0.15s',
                }}>{opt.label}</button>
              );
            })}
          </div>
        ))}
      </ChatPanel>
      </div>
    </div>
  );
}
window.FsmDevicePage = FsmDevicePage;
