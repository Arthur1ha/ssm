function DeviceDetailPage({ unitId, device, unitData, onBack }) {
  const initialMsg = {
    role: 'assistant', agent: unitId,
    text: `你好，我是 ${device?.name || unitId}，有什么可以帮你？`,
  };
  const [messages, setMessages] = React.useState([initialMsg]);
  const onAppend = (msg) => setMessages(prev => [...prev, msg]);
  const { thinking, thinkingText, send } = useSendIntent();

  const meta  = device ? getAgentMeta(device) : { icon: 'bulb', color: '#FF9A5A', label: '设备' };
  const uid   = device?.unit_id || '';
  const ism   = (unitData[uid] || {}).state?.ism || '';
  const isOn  = ism && ism !== 'OFF' && ism !== 'IDLE';
  const n     = (device?.name || '').toLowerCase();

  const isLed = n.includes('led') || n.includes('rgb') || n.includes('ws2812') || n.includes('ring') || n.includes('灯');
  const LED_CMDS = ['开灯', '关灯', '调亮', '调暗', '彩虹', '呼吸灯', '白色'];

  const hasUserMsg = messages.some(m => m.role === 'user');

  const [autonomy, setAutonomy] = React.useState('reactive');

  React.useEffect(() => {
    if (!isLed) return;
    fetch('/api/esp32/autonomy')
      .then(r => r.json())
      .then(d => d.mode && setAutonomy(d.mode))
      .catch(() => {});
  }, [isLed]);

  const switchAutonomy = (mode) => {
    setAutonomy(mode);
    fetch('/api/esp32/autonomy', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    }).catch(() => {});
  };

  const sendChat = (text) => {
    if (!text || thinking) return;
    onAppend({ role: 'user', text });
    send(text, {
      deviceHint:    unitId,
      onMessage:     (msg)  => onAppend({ role: 'assistant', agent: unitId, text: msg }),
      onPendingRule: (rule) => onAppend({ role: 'assistant', agent: unitId,
        text: `收到规则「${rule.name}」，请在主界面确认保存。` }),
    });
  };

  return (
    <div style={{
      position: 'fixed', inset: 0,
      background: 'var(--color-bg)', color: 'var(--color-text)',
      fontFamily: 'var(--font-sans)',
      paddingTop: 'env(safe-area-inset-top, 0px)',
      display: 'flex', flexDirection: 'column',
    }}>

      {/* ── Header（与 Go2 布局一致） ── */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '11px 16px',
        borderBottom: '1px solid var(--color-border)',
        background: 'var(--color-surface-1)',
        flexShrink: 0,
      }}>
        {/* 返回按钮 */}
        <button onClick={onBack} style={{
          background: 'none', border: 'none',
          color: 'var(--color-text-dim)', cursor: 'pointer',
          fontSize: 18, padding: '0 4px', lineHeight: 1,
          fontFamily: 'inherit', WebkitTapHighlightColor: 'transparent',
        }}>←</button>

        {/* 设备名 + 副标题 */}
        <div>
          <div style={{
            fontSize: 13, fontWeight: 700, letterSpacing: '0.12em',
            color: meta.color, textShadow: isOn ? `0 0 12px ${meta.color}80` : 'none',
            transition: 'text-shadow 0.4s',
          }}>{(device?.name || unitId).toUpperCase()}</div>
          <div style={{
            fontSize: 9, color: 'var(--color-text-dim)',
            letterSpacing: '0.18em', marginTop: 1,
          }}>{meta.label.toUpperCase()}</div>
        </div>

        {/* 右侧：状态点 + ISM 文字 + Agent 按钮 */}
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{
            width: 7, height: 7, borderRadius: '50%',
            background: device?._online ? meta.color : 'var(--color-offline)',
            boxShadow: isOn ? `0 0 8px ${meta.color}` : 'none',
            transition: 'box-shadow 0.4s',
          }}/>
          <span style={{
            fontSize: 10, letterSpacing: '0.1em',
            color: device?._online ? meta.color : 'var(--color-text-dim)',
          }}>
            {ism || (device?._online ? 'ONLINE' : 'OFFLINE')}
          </span>
          <a href={'/api/devices/' + unitId + '/agent'} target="_blank" rel="noopener"
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 4,
              background: `${meta.color}12`,
              color: meta.color,
              border: `1px solid ${meta.color}30`,
              borderRadius: 'var(--radius-sm)', padding: '5px 10px',
              fontSize: 10, letterSpacing: '0.1em',
              cursor: 'pointer', textDecoration: 'none',
              fontFamily: 'inherit', WebkitTapHighlightColor: 'transparent',
            }}>
            <Icon name="zap" size={10} color={meta.color}/>
            AGENT
          </a>
        </div>
      </div>

      {/* ── 聊天区（快捷指令通过 children 插在输入栏上方） ── */}
      <ChatPanel
        messages={messages}
        thinking={thinking}
        thinkingText={thinkingText}
        thinkingAgent={unitId}
        onSend={sendChat}
        placeholder="告诉设备要做什么…"
        variant="inline"
      >
        {/* 自主模式切换（仿 Go2，灯专属） */}
        {isLed && (
          <div style={{ display: 'flex', gap: 6, padding: '8px 12px 0' }}>
            {[
              { key: 'reactive', label: '自动调光', icon: '◉' },
              { key: 'manual',   label: '仅听指令', icon: '◎' },
            ].map(({ key, label, icon }) => {
              const active = autonomy === key;
              const accent = key === 'reactive' ? '#00d4ff' : 'var(--color-accent)';
              return (
                <button key={key} onClick={() => switchAutonomy(key)} style={{
                  flex: 1, padding: '7px 4px',
                  background: active ? `${accent}18` : 'var(--color-surface-1)',
                  color: active ? accent : 'var(--color-text-dim)',
                  border: `1px solid ${active ? `${accent}40` : 'var(--color-border)'}`,
                  borderRadius: 'var(--radius-sm)', fontSize: 11, fontWeight: 700,
                  cursor: 'pointer', letterSpacing: '0.07em', fontFamily: 'inherit',
                  WebkitTapHighlightColor: 'transparent', transition: 'all 0.15s',
                }}>{icon} {label}</button>
              );
            })}
          </div>
        )}
        {/* 快捷指令：无用户消息时显示，位置与主屏建议一致 */}
        {isLed && !hasUserMsg && !thinking && (
          <div style={{
            padding: '6px 12px 0',
            display: 'flex', gap: 6,
            overflowX: 'auto', scrollbarWidth: 'none',
          }}>
            {LED_CMDS.map(cmd => (
              <button key={cmd} onClick={() => sendChat(cmd)} style={{
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
  );
}

window.DeviceDetailPage = DeviceDetailPage;
