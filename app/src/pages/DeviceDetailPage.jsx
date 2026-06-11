function DeviceDetailPage({ slug, device, unitData, onBack }) {
  const initialMsg = {
    role: 'assistant', agent: slug,
    agentName: device?.name || slug,
    text: `你好，我是 ${device?.name || slug}，有什么可以帮你？`,
  };
  const [messages, setMessages] = React.useState([initialMsg]);
  const onAppend = (msg) => setMessages(prev => [...prev, msg]);
  const { thinking, thinkingText, send } = useSendIntent();

  const meta     = device ? getAgentMeta(device) : { icon: 'bulb', color: '#FF9A5A' };
  const uid      = device?.unit_id || '';
  const ism      = (unitData[uid] || {}).state?.ism || 'OFF';
  const agentCardUrl = '/api/devices/' + slug + '/agent';

  const sendChat = (text) => {
    if (!text || thinking) return;
    onAppend({ role: 'user', text });
    send(text, {
      deviceHint: slug,
      onMessage:     (msg)  => onAppend({ role: 'assistant', text: msg }),
      onPendingRule: (rule) => onAppend({ role: 'assistant',
        text: `收到规则「${rule.name}」，请在主界面确认保存。` }),
    });
  };

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
      <ChatPanel
        messages={messages}
        thinking={thinking}
        thinkingText={thinkingText}
        onSend={sendChat}
        placeholder="告诉设备要做什么…"
        variant="inline"
      />
    </div>
  );
}

window.DeviceDetailPage = DeviceDetailPage;
