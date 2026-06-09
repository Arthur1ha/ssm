function DeviceDetailPage({ slug, device, unitData, onBack, messages, onAppend }) {
  const [thinking, setThinking] = React.useState(false);

  const meta     = device ? getAgentMeta(device) : { icon: 'bulb', color: '#FF9A5A' };
  const uid      = device?.unit_id || '';
  const ism      = (unitData[uid] || {}).state?.ism || 'OFF';
  const cmdTopic = device?.topics?.command;
  const agentCardUrl = '/api/devices/' + slug + '/agent';

  const sendCmd = (cmd, extra = {}) => {
    if (!cmdTopic) return;
    mqttBus.publish(cmdTopic, { cmd, ...extra });
  };

  const sendChat = (text) => {
    if (!text || thinking) return;
    onAppend({ role: 'user', text });
    setThinking(true);

    const session_id = 'sid_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);
    const feedbackTopic = `ssm/feedback/${session_id}`;
    mqttBus.subscribe(feedbackTopic);

    const cleanup = () => {
      mqttBus.removeEventListener('topic:' + feedbackTopic, handleFeedback);
      mqttBus.unsubscribe(feedbackTopic);
    };

    const timeoutId = setTimeout(() => {
      cleanup();
      setThinking(false);
      onAppend({ role: 'assistant', text: '操作超时，设备可能无响应' });
    }, 60000);

    function handleFeedback(e) {
      const { stage, text: msg, rule } = e.detail || {};
      if (!stage) return;
      if (stage === 'pending_rule' && rule) {
        clearTimeout(timeoutId);
        cleanup();
        setThinking(false);
        onAppend({ role: 'assistant', text: `收到规则「${rule.name}」，请在主界面确认保存。` });
      } else if (stage === 'done' || stage === 'partial' || stage === 'failed') {
        clearTimeout(timeoutId);
        cleanup();
        setThinking(false);
        onAppend({ role: 'assistant', text: msg || '已处理' });
      }
    }
    mqttBus.addEventListener('topic:' + feedbackTopic, handleFeedback);

    mqttBus.publish(`ssm/intent/${session_id}`, JSON.stringify({
      session_id,
      user_msg: text,
      ts: Date.now(),
    }));
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
      <ChatPanel
        messages={messages}
        thinking={thinking}
        onSend={sendChat}
        placeholder="告诉设备要做什么…"
        variant="inline"
      />
    </div>
  );
}

window.DeviceDetailPage = DeviceDetailPage;
