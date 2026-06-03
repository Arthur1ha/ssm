/* SSM PWA — root app: routing, chat sheet, device detail, MQTT bootstrap */
const { useState, useEffect, useRef, useCallback } = React;

/* ── Hash 路由 ──────────────────────────────────────────────────── */
function useHash() {
  const [hash, setHash] = React.useState(window.location.hash);
  React.useEffect(() => {
    const handler = () => setHash(window.location.hash);
    window.addEventListener('hashchange', handler);
    return () => window.removeEventListener('hashchange', handler);
  }, []);
  return hash;
}

/* ── TTS 音频播放 ────────────────────────────────────────────────── */
// Android Chrome 要求用户首次手势后解锁 AudioContext，
// 之后 MQTT 触发的 Audio.play() 才不会被自动播放策略拦截。
let _audioUnlocked = false;
function _unlockAudio() {
  if (_audioUnlocked) return;
  _audioUnlocked = true;
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    ctx.resume().then(() => ctx.close()).catch(() => {});
  } catch (e) {}
}
document.addEventListener('pointerdown', _unlockAudio, { passive: true });

function _base64ToBlob(b64, mimeType) {
  const bin   = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: mimeType });
}

function playAudioB64(b64) {
  if (!b64) return;
  try {
    const blob  = _base64ToBlob(b64, 'audio/mpeg');
    const url   = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.onended = () => URL.revokeObjectURL(url);
    audio.onerror = () => URL.revokeObjectURL(url);
    audio.play().catch(err => console.warn('[Speech] play() blocked:', err));
  } catch (e) {
    console.warn('[Speech] playAudioB64 error:', e);
  }
}

/* ── ChatSheet — bottom sheet，跨开关保持对话上下文 ─────────────── */
const SUGGESTIONS = ['开灯，暖白', '红色 LED', '播放通知音', '关闭 LED'];

function ChatSheet({ open, onClose, agents, unitData }) {
  const actuatorsRef = useRef([]);
  actuatorsRef.current = agents.filter(a => a.agent_type === 'actuator');
  const subs = actuatorsRef.current;

  const [messages, setMessages]         = useState([{ role: 'assistant', text: '需要控制什么设备？', actions: [] }]);
  const [input, setInput]               = useState('');
  const [thinking, setThinking]         = useState(false);
  const [thinkingText, setThinkingText] = useState('');
  const [kbOffset, setKbOffset]         = useState(0);
  const [pendingRule, setPendingRule]   = useState(null);
  const [savingRule, setSavingRule]     = useState(false);
  const endRef   = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 360);
    if (!open) setKbOffset(0);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const vv = window.visualViewport;
    if (!vv) return;
    const update = () => setKbOffset(Math.max(0, window.innerHeight - vv.height));
    vv.addEventListener('resize', update);
    vv.addEventListener('scroll', update);
    return () => { vv.removeEventListener('resize', update); vv.removeEventListener('scroll', update); };
  }, [open]);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages, thinking, kbOffset]);

  const handleConfirmRule = async () => {
    if (!pendingRule) return;
    setSavingRule(true);
    try {
      await fetch('/api/rules', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(pendingRule),
      });
      const saved = pendingRule;
      setPendingRule(null);
      setMessages(m => [...m, { role: 'assistant', text: `规则「${saved.name}」已保存，条件触发时自动执行。`, actions: [] }]);
    } catch {
      setMessages(m => [...m, { role: 'assistant', text: '规则保存失败，请重试。', actions: [] }]);
    }
    setSavingRule(false);
  };

  const handleCancelRule = () => {
    setPendingRule(null);
    setMessages(m => [...m, { role: 'assistant', text: '已取消，规则未保存。', actions: [] }]);
  };

  const send = async (text) => {
    const t = (text || input).trim();
    if (!t) return;
    setInput('');
    setPendingRule(null);
    setMessages(m => [...m, { role: 'user', text: t }]);

    if (subs.length === 0) {
      setMessages(m => [...m, { role: 'assistant', text: '附近没有发现可控设备，请确认 ESP32 已上线。', actions: [] }]);
      return;
    }

    setThinking(true);
    setThinkingText('解析意图...');

    try {
      const res = await fetch('/api/intent', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: t, devices: subs }),
      });
      if (!res.ok) throw new Error('nlu_failed');
      const nluData = await res.json();
      const { session_id, nlu_feedback, intent_type, requirements, rule } = nluData;

      setMessages(m => [...m, { role: 'assistant', text: nlu_feedback, actions: [] }]);
      setThinking(false);
      setThinkingText('');

      if (intent_type === 'define_rule' && rule) {
        setPendingRule(rule);
        return;
      }

      setThinking(true);
      setThinkingText('正在规划...');
      const feedbackTopic = `ssm/feedback/${session_id}`;
      mqttBus.subscribe(feedbackTopic);

      let timeoutId = setTimeout(() => {
        mqttBus.removeEventListener('topic:' + feedbackTopic, handleFeedback);
        setThinking(false);
        setThinkingText('');
        setMessages(m => [...m, { role: 'assistant', text: '操作超时，设备可能无响应', actions: [] }]);
      }, 60000);

      function handleFeedback(e) {
        const { stage, text } = e.detail || {};
        if (!stage) return;
        if (stage === 'planning' || stage === 'executing') {
          clearTimeout(timeoutId);
          timeoutId = setTimeout(() => {
            mqttBus.removeEventListener('topic:' + feedbackTopic, handleFeedback);
            setThinking(false);
            setThinkingText('');
            setMessages(m => [...m, { role: 'assistant', text: '操作超时，设备可能无响应', actions: [] }]);
          }, 40000);
          setThinkingText(stage === 'planning' ? '正在规划...' : '正在执行...');
        } else if (stage === 'done' || stage === 'partial' || stage === 'failed') {
          clearTimeout(timeoutId);
          mqttBus.removeEventListener('topic:' + feedbackTopic, handleFeedback);
          setThinking(false);
          setThinkingText('');
          setMessages(m => [...m, { role: 'assistant', text, actions: [] }]);
        }
      }
      mqttBus.addEventListener('topic:' + feedbackTopic, handleFeedback);

      mqttBus.publish(`ssm/intent/${session_id}`, {
        session_id,
        user_msg: t,
        requirements,
        priority: 5,
        ts: Math.floor(Date.now() / 1000),
      });

    } catch (e) {
      setThinking(false);
      setThinkingText('');
      setMessages(m => [...m, { role: 'assistant', text: '服务暂时无响应，请稍后重试。', actions: [] }]);
    }
  };

  return (
    <>
      <div onClick={onClose} style={{
        position: 'fixed', inset: 0, zIndex: 200,
        background: 'rgba(0,0,0,0.55)',
        opacity: open ? 1 : 0,
        transition: 'opacity 0.3s',
        pointerEvents: open ? 'auto' : 'none',
      }}/>
      <div style={{
        position: 'fixed', left: 0, right: 0,
        bottom: kbOffset,
        height: kbOffset > 0 ? `calc(100vh - ${kbOffset}px - 20px)` : '82vh',
        zIndex: 201,
        background: '#0F0F14',
        borderRadius: '22px 22px 0 0',
        border: '1px solid rgba(255,255,255,0.08)',
        boxShadow: '0 -20px 60px rgba(0,0,0,0.5)',
        transform: open ? 'translateY(0)' : 'translateY(100%)',
        transition: 'transform 0.38s cubic-bezier(0.32, 0.72, 0, 1), bottom 0.22s ease, height 0.22s ease',
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
      }}>
        <div style={{ padding: '10px 0 2px', display: 'flex', justifyContent: 'center', flexShrink: 0 }}>
          <div style={{ width: 36, height: 4, borderRadius: 2, background: 'rgba(255,255,255,0.12)' }}/>
        </div>
        <div style={{ padding: '6px 20px 10px', display: 'flex', alignItems: 'center', gap: 10,
          flexShrink: 0, borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
          <div style={{ width: 30, height: 30, borderRadius: 10, background: LIME, color: '#0B0B0E',
            display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 16, flexShrink: 0 }}>◐</div>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 14, fontWeight: 500 }}>SSM 助手</div>
            <div style={{ fontSize: 11, color: LIME, fontFamily: 'monospace' }}>{subs.length} 个设备</div>
          </div>
          <button onClick={onClose} style={{
            width: 30, height: 30, borderRadius: '50%', border: '1px solid rgba(255,255,255,0.09)',
            background: 'rgba(255,255,255,0.06)', color: 'rgba(255,255,255,0.6)',
            cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 0,
          }}>
            <Icon name="x" size={14}/>
          </button>
        </div>
        <div style={{ flex: 1, overflowY: 'auto', padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: 10 }}>
          {messages.map((m, i) => (
            <div key={i} style={{ maxWidth: '82%', alignSelf: m.role==='user'?'flex-end':'flex-start', display:'flex', flexDirection:'column', gap:6 }}>
              <div style={{
                padding: '10px 14px', fontSize: 14, lineHeight: 1.5,
                borderRadius: m.role==='user' ? '18px 18px 4px 18px' : '4px 18px 18px 18px',
                background: m.role==='user' ? LIME : 'rgba(255,255,255,0.06)',
                color: m.role==='user' ? '#0B0B0E' : '#fff',
                border: m.role==='user' ? 'none' : '1px solid rgba(255,255,255,0.07)',
              }}>{m.text}</div>
              {m.actions?.map((ac, ai) => (
                <div key={ai} style={{ display:'flex', alignItems:'center', gap:8, padding:'7px 11px',
                  background:'rgba(200,255,62,0.06)', border:'1px solid rgba(200,255,62,0.2)', borderRadius:12 }}>
                  <div style={{ width:22, height:22, borderRadius:7, background:LIME, color:'#0B0B0E',
                    display:'flex', alignItems:'center', justifyContent:'center' }}>
                    <Icon name="check" size={11} sw={2.5}/>
                  </div>
                  <span style={{ fontSize:12, color:'rgba(255,255,255,0.55)' }}>{ac.name}</span>
                  <span style={{ fontSize:12, color:LIME, fontFamily:'monospace', marginLeft:'auto' }}>{ac.action}</span>
                </div>
              ))}
            </div>
          ))}
          {thinking && (
            <div style={{ alignSelf:'flex-start', padding:'10px 14px', borderRadius:'4px 18px 18px 18px',
              background:'rgba(255,255,255,0.06)', border:'1px solid rgba(255,255,255,0.07)',
              display:'flex', flexDirection:'column', gap:6 }}>
              <div style={{ display:'flex', gap:4, alignItems:'center' }}>
                <span className="typing-dot"/><span className="typing-dot" style={{ animationDelay:'.14s' }}/><span className="typing-dot" style={{ animationDelay:'.28s' }}/>
              </div>
              {thinkingText && (
                <span style={{ fontSize:11, color:'rgba(255,255,255,0.32)', fontFamily:'monospace' }}>
                  {thinkingText}
                </span>
              )}
            </div>
          )}
          <div ref={endRef}/>
        </div>
        {pendingRule && (
          <div style={{ padding: '0 16px 10px', flexShrink: 0 }}>
            <div style={{ background: 'rgba(200,255,62,0.07)', border: '1px solid rgba(200,255,62,0.22)',
              borderRadius: 18, padding: '14px 16px' }}>
              <div style={{ fontSize: 12, color: LIME, fontWeight: 600, marginBottom: 8 }}>规则预览 · 确认保存？</div>
              <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 4 }}>{pendingRule.name}</div>
              <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.4)', fontFamily: 'monospace', marginBottom: 12 }}>
                当 {pendingRule.trigger?.agent_tag}.{pendingRule.trigger?.event}
                {' → '}
                {pendingRule.action?.resource_tag} / {pendingRule.action?.cmd}
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <button onClick={handleCancelRule}
                  style={{ flex: 1, padding: '9px 0', borderRadius: 999, fontSize: 13,
                    background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.09)',
                    color: 'rgba(255,255,255,0.5)', cursor: 'pointer', fontFamily: 'inherit' }}>
                  取消
                </button>
                <button onClick={handleConfirmRule} disabled={savingRule}
                  style={{ flex: 2, padding: '9px 0', borderRadius: 999, fontSize: 13, fontWeight: 600,
                    background: LIME, border: 'none', color: '#0B0B0E',
                    cursor: 'pointer', fontFamily: 'inherit',
                    boxShadow: '0 0 16px rgba(200,255,62,0.3)' }}>
                  {savingRule ? '保存中...' : '确认保存'}
                </button>
              </div>
            </div>
          </div>
        )}
        {messages.length <= 1 && (
          <div style={{ padding:'0 12px 8px', display:'flex', gap:6, overflowX:'auto', scrollbarWidth:'none', flexShrink:0 }}>
            {SUGGESTIONS.map(s => (
              <button key={s} onClick={() => send(s)} style={{
                padding:'6px 12px', borderRadius:999, whiteSpace:'nowrap',
                background:'rgba(255,255,255,0.06)', border:'1px solid rgba(255,255,255,0.09)',
                color:'rgba(255,255,255,0.6)', fontSize:12, cursor:'pointer', fontFamily:'inherit',
              }}>{s}</button>
            ))}
          </div>
        )}
        <div style={{ padding:'8px 12px', paddingBottom:'calc(12px + env(safe-area-inset-bottom, 0px))',
          borderTop:'1px solid rgba(255,255,255,0.05)', flexShrink:0 }}>
          <div style={{ display:'flex', alignItems:'center', gap:8, padding:'6px 6px 6px 16px',
            background:'rgba(30,29,38,0.95)', border:'1px solid rgba(255,255,255,0.09)', borderRadius:999 }}>
            <input ref={inputRef} value={input} onChange={e => setInput(e.target.value)}
              onKeyDown={e => e.key==='Enter' && send()}
              placeholder="告诉设备要做什么…"
              style={{ flex:1, background:'transparent', border:'none', color:'#fff', fontSize:14, fontFamily:'inherit', outline:'none' }}/>
            <button onClick={() => send()} disabled={!input.trim()} style={{
              width:38, height:38, borderRadius:999,
              background: input.trim() ? LIME : 'rgba(255,255,255,0.08)',
              color: input.trim() ? '#0B0B0E' : 'rgba(255,255,255,0.25)',
              border:'none', display:'flex', alignItems:'center', justifyContent:'center',
              cursor: input.trim() ? 'pointer' : 'default',
              boxShadow: input.trim() ? '0 0 18px rgba(200,255,62,0.35)' : 'none', flexShrink:0,
            }}>
              <Icon name="arrow" size={16} sw={2.2}/>
            </button>
          </div>
        </div>
      </div>
    </>
  );
}

/* ── PersistentInputBar ─────────────────────────────────────────── */
function PersistentInputBar({ onOpen }) {
  return (
    <div onClick={onOpen} style={{
      position: 'absolute', left: 12, right: 12,
      bottom: 'calc(86px + env(safe-area-inset-bottom, 0px))',
      display: 'flex', alignItems: 'center', gap: 10,
      padding: '10px 10px 10px 16px',
      background: 'rgba(22,21,28,0.94)',
      backdropFilter: 'blur(28px) saturate(140%)',
      border: '1px solid rgba(255,255,255,0.09)',
      borderRadius: 999,
      cursor: 'text',
      boxShadow: '0 4px 24px rgba(0,0,0,0.35)',
      zIndex: 10,
    }}>
      <span style={{ fontSize: 17, lineHeight: 1 }}>◐</span>
      <span style={{ flex: 1, fontSize: 14, color: 'rgba(255,255,255,0.3)', userSelect: 'none' }}>
        告诉我想要什么…
      </span>
      <div style={{ width: 34, height: 34, borderRadius: 999, flexShrink: 0,
        background: `${LIME}15`, border: `1px solid ${LIME}33`,
        display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Icon name="arrow" size={14} color={LIME}/>
      </div>
    </div>
  );
}

/* ── DeviceDiscoveryCard — GPS 附近弹出 ─────────────────────────── */
function DeviceDiscoveryCard({ agent, unitData, onDismiss, onGo }) {
  const meta    = getAgentMeta(agent);
  const reading = agent.agent_type !== 'actuator'
    ? getSensorReading(agent, unitData)
    : { value: getStateLabel(agent, unitData), color: meta.color };

  return (
    <div style={{
      position: 'absolute', left: 0, right: 0, bottom: 0, zIndex: 50,
      background: 'rgba(14,13,20,0.97)',
      backdropFilter: 'blur(32px) saturate(150%)',
      borderTop: '1px solid rgba(255,255,255,0.1)',
      borderRadius: '24px 24px 0 0',
      padding: '16px 20px calc(20px + env(safe-area-inset-bottom, 0px))',
    }}>
      <div style={{ width: 36, height: 4, borderRadius: 2, background: 'rgba(255,255,255,0.15)', margin: '0 auto 18px' }}/>
      <div style={{ fontSize: 10, color: 'rgba(255,255,255,0.3)', textAlign: 'center',
        fontFamily: 'monospace', letterSpacing: '0.1em', marginBottom: 16 }}>附近发现设备</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginBottom: 22 }}>
        <div style={{ width: 52, height: 52, borderRadius: 16, flexShrink: 0,
          background: `${meta.color}18`, color: meta.color,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          border: `1px solid ${meta.color}30` }}>
          <Icon name={meta.icon} size={24}/>
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 16, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {agent.name || agent.unit_id}
          </div>
          <div style={{ fontSize: 12, color: 'rgba(255,255,255,0.38)', marginTop: 3 }}>
            {meta.label} · {agent.agent_type === 'actuator' ? '执行器' : '传感器'}
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: reading.color }}>{reading.value}</span>
          <div style={{ width: 8, height: 8, borderRadius: '50%',
            background: agent._online ? LIME : 'rgba(255,255,255,0.2)',
            boxShadow: agent._online ? `0 0 6px ${LIME}` : 'none' }}/>
        </div>
      </div>
      <div style={{ display: 'flex', gap: 10 }}>
        <button onClick={onDismiss} style={{
          flex: 1, padding: '13px 0', borderRadius: 14, fontSize: 14, fontWeight: 500,
          background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.1)',
          color: 'rgba(255,255,255,0.55)', cursor: 'pointer', fontFamily: 'inherit',
        }}>稍后</button>
        <button onClick={onGo} style={{
          flex: 2, padding: '13px 0', borderRadius: 14, fontSize: 14, fontWeight: 600,
          background: LIME, border: 'none', color: '#0B0B0E', cursor: 'pointer', fontFamily: 'inherit',
        }}>前往设备 →</button>
      </div>
    </div>
  );
}

/* ── TabBar ──────────────────────────────────────────────────────── */
function TabBar({ tab, setTab, deviceBadge }) {
  const tabs = [
    { id: 'discover', icon: 'search', label: '附近' },
    { id: 'devices',  icon: 'home',   label: '设备', badge: deviceBadge },
    { id: 'rules',    icon: 'list',   label: '规则' },
  ];
  return (
    <div style={{
      position: 'absolute', left: 12, right: 12,
      bottom: 'calc(10px + env(safe-area-inset-bottom, 0px))',
      background: 'rgba(18,17,22,0.88)', backdropFilter: 'blur(24px) saturate(140%)',
      border: '1px solid rgba(255,255,255,0.09)', borderRadius: 24,
      padding: 8, display: 'flex', gap: 4,
      boxShadow: '0 16px 40px rgba(0,0,0,0.5)',
    }}>
      {tabs.map(t => {
        const active = tab === t.id;
        return (
          <button key={t.id} onClick={() => setTab(t.id)} style={{
            flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 3,
            padding: '8px 4px', borderRadius: 16,
            background: active ? 'rgba(255,255,255,0.07)' : 'transparent',
            color: active ? '#fff' : 'rgba(255,255,255,0.38)',
            border: 'none', cursor: 'pointer', position: 'relative', fontFamily: 'inherit',
          }}>
            <Icon name={t.icon} size={18}/>
            <span style={{ fontSize: 10, fontWeight: 500 }}>{t.label}</span>
            {t.badge > 0 && !active && (
              <span style={{ position:'absolute', top:6, right:'26%',
                minWidth:16, height:16, padding:'0 4px', borderRadius:999,
                background:LIME, color:'#0B0B0E', fontSize:10, fontWeight:600,
                display:'flex', alignItems:'center', justifyContent:'center' }}>{t.badge}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/* ── DeviceDetailPage — 单设备全屏控制 + 对话 ───────────────────── */
function DeviceDetailPage({ slug, device, unitData, onBack }) {
  const [messages, setMessages] = React.useState([
    { role: 'assistant', text: device ? `你好，我是 ${device.name}，有什么可以帮你？` : '设备连接中…' }
  ]);
  const [input, setInput]       = React.useState('');
  const [thinking, setThinking] = React.useState(false);
  const endRef   = React.useRef(null);
  const inputRef = React.useRef(null);

  React.useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages, thinking]);
  React.useEffect(() => { setTimeout(() => inputRef.current?.focus(), 300); }, []);

  const meta     = device ? getAgentMeta(device) : { icon: 'bulb', color: '#FF9A5A' };
  const uid      = device?.unit_id || '';
  const ism      = (unitData[uid] || {}).state?.ism || 'OFF';
  const cmdTopic = device?.topics?.command;
  const agentCardUrl = '/api/devices/' + slug + '/agent';

  const sendCmd = (cmd, extra = {}) => {
    if (!cmdTopic) return;
    mqttBus.publish(cmdTopic, { cmd, ...extra });
  };

  const sendChat = async () => {
    const msg = input.trim();
    if (!msg || thinking) return;
    setInput('');
    setMessages(prev => [...prev, { role: 'user', text: msg }]);
    setThinking(true);
    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: msg,
          devices: device ? [{
            unit_id:      uid,
            agent_type:   'actuator',
            topics:       { command: cmdTopic },
            capabilities: device.capabilities || [],
          }] : [],
        }),
      });
      const data = await res.json();
      setMessages(prev => [...prev, { role: 'assistant', text: data.reply || '已处理' }]);
    } catch (e) {
      setMessages(prev => [...prev, { role: 'assistant', text: '请求失败：' + e.message }]);
    }
    setThinking(false);
  };

  const btnBase = {
    flex: 1, padding: '7px 4px', borderRadius: 999, fontSize: 12,
    cursor: 'pointer', fontFamily: 'inherit', border: 'none',
    transition: 'background 0.15s',
  };
  const btnOn  = { ...btnBase, background: meta.color, color: '#0B0B0E', fontWeight: 600,
    boxShadow: `0 0 10px ${meta.color}60` };
  const btnOff = { ...btnBase, background: 'rgba(255,255,255,0.07)',
    border: '1px solid rgba(255,255,255,0.09)', color: 'rgba(255,255,255,0.55)' };

  return (
    <div style={{
      position: 'fixed', inset: 0, background: '#0B0B0E', color: '#fff',
      fontFamily: '-apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif',
      paddingTop: 'env(safe-area-inset-top, 0px)',
      display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '12px 16px',
        borderBottom: '1px solid rgba(255,255,255,0.06)', flexShrink: 0 }}>
        <button onClick={onBack} style={{
          background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.1)',
          color: 'rgba(255,255,255,0.7)', borderRadius: 10, padding: '6px 12px',
          cursor: 'pointer', fontFamily: 'inherit', fontSize: 13,
          display: 'flex', alignItems: 'center', gap: 5,
        }}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M19 12H5M12 5l-7 7 7 7"/>
          </svg>
          返回
        </button>
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', gap: 10 }}>
          <div style={{ width: 34, height: 34, borderRadius: 10, flexShrink: 0,
            background: `${meta.color}18`, color: meta.color,
            display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <Icon name={meta.icon} size={17}/>
          </div>
          <div>
            <div style={{ fontSize: 15, fontWeight: 600 }}>{device?.name || slug}</div>
            <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.35)', fontFamily: 'monospace' }}>{ism}</div>
          </div>
        </div>
        <a href={agentCardUrl} target="_blank" rel="noopener" style={{
          padding: '6px 12px', borderRadius: 10, fontSize: 12,
          background: `${meta.color}15`, border: `1px solid ${meta.color}35`,
          color: meta.color, textDecoration: 'none',
          display: 'flex', alignItems: 'center', gap: 5, flexShrink: 0,
        }}>
          <Icon name="zap" size={12} color={meta.color}/>
          Agent 接入
        </a>
      </div>
      {device && (
        <div style={{ padding: '12px 16px', borderBottom: '1px solid rgba(255,255,255,0.06)', flexShrink: 0 }}>
          <div style={{ display: 'flex', gap: 6 }}>
            <button onClick={() => sendCmd('SET_STATE', { state: ism === 'OFF' ? 'BRIGHT' : 'OFF' })}
              style={ism !== 'OFF' ? btnOn : btnOff}>
              {ism === 'OFF' ? '开灯' : '关灯'}
            </button>
            <button onClick={() => sendCmd('SET_STATE', { state: 'DIM' })}
              style={ism === 'DIM' ? btnOn : btnOff}>微光</button>
            <button onClick={() => sendCmd('SET_COLOR', { r: 255, g: 160, b: 60, brightness: 180 })}
              style={ism === 'COLOR' ? btnOn : btnOff}>暖黄</button>
            <button onClick={() => sendCmd('BLINK', { r: 255, g: 180, b: 30, count: 3 })}
              style={ism === 'BLINK' ? btnOn : btnOff}>闪烁</button>
          </div>
        </div>
      )}
      <div style={{ flex: 1, overflowY: 'auto', padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: 10 }}>
        {messages.map((m, i) => (
          <div key={i} style={{ maxWidth: '82%', alignSelf: m.role === 'user' ? 'flex-end' : 'flex-start' }}>
            <div style={{
              padding: '10px 14px', fontSize: 14, lineHeight: 1.5,
              borderRadius: m.role === 'user' ? '18px 18px 4px 18px' : '4px 18px 18px 18px',
              background: m.role === 'user' ? LIME : 'rgba(255,255,255,0.06)',
              color: m.role === 'user' ? '#0B0B0E' : '#fff',
              border: m.role === 'user' ? 'none' : '1px solid rgba(255,255,255,0.07)',
            }}>{m.text}</div>
          </div>
        ))}
        {thinking && (
          <div style={{ alignSelf: 'flex-start', padding: '10px 14px',
            borderRadius: '4px 18px 18px 18px',
            background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.07)',
            display: 'flex', gap: 4, alignItems: 'center' }}>
            <span className="typing-dot"/>
            <span className="typing-dot" style={{ animationDelay: '.14s' }}/>
            <span className="typing-dot" style={{ animationDelay: '.28s' }}/>
          </div>
        )}
        <div ref={endRef}/>
      </div>
      <div style={{ padding: '8px 12px', paddingBottom: 'calc(8px + env(safe-area-inset-bottom, 0px))',
        borderTop: '1px solid rgba(255,255,255,0.06)', flexShrink: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 6px 6px 16px',
          background: 'rgba(30,29,38,0.95)', border: '1px solid rgba(255,255,255,0.09)', borderRadius: 999 }}>
          <input
            ref={inputRef}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && !e.isComposing && sendChat()}
            placeholder="告诉灯要做什么…"
            style={{ flex: 1, background: 'transparent', border: 'none',
              color: '#fff', fontSize: 14, fontFamily: 'inherit', outline: 'none' }}
          />
          <button onClick={sendChat} disabled={!input.trim() || thinking} style={{
            width: 38, height: 38, borderRadius: 999, flexShrink: 0,
            background: input.trim() && !thinking ? LIME : 'rgba(255,255,255,0.08)',
            color:      input.trim() && !thinking ? '#0B0B0E' : 'rgba(255,255,255,0.25)',
            border: 'none', cursor: input.trim() && !thinking ? 'pointer' : 'default',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            boxShadow: input.trim() && !thinking ? '0 0 18px rgba(200,255,62,0.35)' : 'none',
          }}>
            <Icon name="arrow" size={16} sw={2.2}/>
          </button>
        </div>
      </div>
    </div>
  );
}

/* ── App ─────────────────────────────────────────────────────────── */
const EXCL_TYPES = new Set(['decision', 'supervisor']);
const EXCL_PLAT  = new Set(['pc', 'pwa']);

const GO2_STATIC_DEVICE = {
  unit_id:      "go2",
  agent_id:     "go2",
  slug:         "go2",
  name:         "Go2 Air",
  agent_type:   "robot",
  capabilities: ["MOVE", "STAND_UP", "SIT_DOWN", "HELLO", "STRETCH", "DANCE"],
};

function App() {
  const [tab, setTab]                         = useState('discover');
  const [sheetOpen, setSheetOpen]             = useState(false);
  const [connected, setConnected]             = useState(false);
  const [agents, setAgents]                   = useState([GO2_STATIC_DEVICE]);
  const [unitData, setUnitData]               = useState({});
  const [phoneLoc, setPhoneLoc]               = useState(null);
  const [locError, setLocError]               = useState(null);
  const [discoveryDevice, setDiscoveryDevice] = useState(null);
  const currentHash        = useHash();
  const seenPopupDevices   = useRef(new Set());
  const agentsRef          = useRef([]);
  const phoneLocRef        = useRef(null);
  const onlineIdsRef       = useRef(new Set());

  useEffect(() => { agentsRef.current  = agents;   }, [agents]);
  useEffect(() => { phoneLocRef.current = phoneLoc; }, [phoneLoc]);

  useEffect(() => {
    const loc = phoneLocRef.current;
    const currentIds = new Set(agents.map(a => a.unit_id || a.agent_id));
    currentIds.forEach(uid => {
      if (!onlineIdsRef.current.has(uid)) seenPopupDevices.current.delete(uid);
    });
    onlineIdsRef.current = currentIds;
    if (!loc) return;
    agents.forEach(agent => {
      if (agent._lat == null || agent._lng == null) return;
      const uid = agent.unit_id || agent.agent_id;
      if (seenPopupDevices.current.has(uid)) return;
      const dist = haversine(loc.lat, loc.lng, agent._lat, agent._lng);
      if (dist < POPUP_RADIUS_M) {
        seenPopupDevices.current.add(uid);
        setDiscoveryDevice(prev => prev || agent);
      }
    });
  }, [agents]);

  useEffect(() => {
    if (!navigator.geolocation) { setLocError('浏览器不支持定位'); return; }
    const watchId = navigator.geolocation.watchPosition(
      pos => {
        const loc = { lat: pos.coords.latitude, lng: pos.coords.longitude };
        setPhoneLoc(loc);
        console.log('[GPS] phone loc:', loc, 'agents:', agentsRef.current.length);
        agentsRef.current.forEach(agent => {
          const uid = agent.unit_id || agent.agent_id;
          if (agent._lat == null || agent._lng == null) { console.log('[GPS] skip (no loc):', uid); return; }
          const dist = Math.round(haversine(loc.lat, loc.lng, agent._lat, agent._lng));
          console.log('[GPS]', uid, 'dist:', dist, 'm / threshold:', POPUP_RADIUS_M, 'm | seen:', seenPopupDevices.current.has(uid));
          if (seenPopupDevices.current.has(uid)) return;
          if (dist < POPUP_RADIUS_M) {
            seenPopupDevices.current.add(uid);
            setDiscoveryDevice(prev => prev || agent);
          }
        });
      },
      err => { console.warn('[GPS] error:', err.code, err.message); setLocError(err.code === 1 ? '位置权限被拒绝' : '定位失败'); },
      { enableHighAccuracy: true, timeout: 10000 }
    );
    return () => navigator.geolocation.clearWatch(watchId);
  }, []);

  useEffect(() => {
    const registry   = new AgentRegistry(mqttBus);
    const ismTracker = new ISMTracker(mqttBus);

    registry.addEventListener('change', () => {
      const mqttAgents = registry.getAll().filter(a =>
        a.agent_type && !EXCL_TYPES.has(a.agent_type) && !EXCL_PLAT.has(a.hw_platform) && a._online === true
      );
      setAgents([GO2_STATIC_DEVICE, ...mqttAgents]);
    });

    registry.addEventListener('reconnect', ({ detail }) => {
      agentsRef.current.forEach(agent => {
        if (agent.parent_id === detail.parentId) {
          const uid = agent.unit_id || agent.agent_id;
          seenPopupDevices.current.delete(uid);
          onlineIdsRef.current.delete(uid);
        }
      });
    });

    let pendingUnitData = false;
    ismTracker.addEventListener('update', () => {
      if (pendingUnitData) return;
      pendingUnitData = true;
      requestAnimationFrame(() => {
        pendingUnitData = false;
        const snap = {};
        ismTracker.unitIds().forEach(uid => {
          snap[uid] = {
            state:  ismTracker.get(uid, 'state'),
            event:  ismTracker.get(uid, 'event'),
            report: ismTracker.get(uid, 'report'),
          };
        });
        setUnitData({ ...snap });
      });
    });

    mqttBus.onConnect(() => {
      setConnected(true);
      mqttBus.publish('ssm/agents/phone_ui/manifest', {
        unit_id: 'phone_ui', agent_type: 'supervisor',
        name: 'human_supervisor', hw_platform: 'pwa',
        ts: Math.floor(Date.now() / 1000),
      }, { retain: true });
      mqttBus.subscribe('ssm/agents/desk/speech');
    });
    mqttBus.addEventListener('disconnect', () => setConnected(false));
    mqttBus.addEventListener('reconnect',  () => setConnected(false));

    mqttBus.connect(BROKER_URL, null, { username: BROKER_USER, password: BROKER_PASS });
  }, []);

  useEffect(() => {
    const handleSpeech = (e) => {
      const { audio } = e.detail || {};
      if (audio) playAudioB64(audio);
    };
    mqttBus.addEventListener('topic:ssm/agents/desk/speech', handleSpeech);
    return () => mqttBus.removeEventListener('topic:ssm/agents/desk/speech', handleSpeech);
  }, []);

  const hashMatch = currentHash.match(/^#\/devices\/([^/]+)$/);
  if (hashMatch) {
    const slug = hashMatch[1];
    if (slug === "go2") {
      return <Go2DevicePage onBack={() => navigate('#')} />;
    }
    const device = agents.find(a => a.slug === slug || (a.unit_id || a.agent_id) === slug);
    if (!device) { navigate('#'); return null; }
    return (
      <DeviceDetailPage
        slug={slug}
        device={device}
        unitData={unitData}
        onBack={() => navigate('#')}
      />
    );
  }

  return (
    <div style={{
      position: 'fixed', inset: 0, overflow: 'hidden',
      background: '#0B0B0E', color: '#fff',
      fontFamily: '-apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif',
      paddingTop: 'env(safe-area-inset-top, 0px)',
    }}>
      <div style={{ position: 'relative', height: '100%' }}>
        {tab === 'discover' && (
          <DiscoverScreen agents={agents} connected={connected} phoneLoc={phoneLoc} locError={locError}/>
        )}
        {tab === 'devices' && (
          <DevicesScreen agents={agents} unitData={unitData}/>
        )}
        {tab === 'rules' && <RulesScreen />}
        <PersistentInputBar onOpen={() => setSheetOpen(true)}/>
        <TabBar tab={tab} setTab={setTab} deviceBadge={agents.length}/>
        {discoveryDevice && (
          <div style={{ position: 'absolute', inset: 0, zIndex: 40,
            background: 'rgba(0,0,0,0.45)', backdropFilter: 'blur(3px)' }}
            onClick={() => setDiscoveryDevice(null)}>
            <div onClick={e => e.stopPropagation()}>
              <DeviceDiscoveryCard
                agent={discoveryDevice}
                unitData={unitData}
                onDismiss={() => setDiscoveryDevice(null)}
                onGo={() => { setTab('devices'); setDiscoveryDevice(null); }}
              />
            </div>
          </div>
        )}
      </div>
      <ChatSheet
        open={sheetOpen}
        onClose={() => setSheetOpen(false)}
        agents={agents}
        unitData={unitData}
      />
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
