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
      const res = await fetch('/api/intent', {
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
      setMessages(prev => [...prev, { role: 'assistant', text: data.nlu_feedback || '已处理' }]);
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

window.DeviceDetailPage = DeviceDetailPage;
