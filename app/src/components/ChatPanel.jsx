/* ChatPanel — 统一聊天面板组件（variant="inline"：内嵌页面，撑满父容器剩余高度） */
const { useState, useEffect, useRef } = React;

function ChatPanel({ messages, thinking, thinkingText, onSend, placeholder,
                     variant, disabled, open, children }) {
  const [input,  setInput]  = useState('');
  const msgsRef  = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    if (msgsRef.current) {
      msgsRef.current.scrollTop = msgsRef.current.scrollHeight;
    }
  }, [messages, thinking]);

  // sheet variant: 随 open 变化聚焦输入框
  useEffect(() => {
    if (variant === 'sheet' && open) {
      setTimeout(() => inputRef.current?.focus(), 360);
    }
  }, [open, variant]);

  const handleSend = () => {
    const t = input.trim();
    if (!t || disabled) return;
    setInput('');
    onSend(t);
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, overflow: 'hidden' }}>

      {/* 消息气泡区 */}
      <div ref={msgsRef} style={{ flex: 1, overflowY: 'auto', padding: '12px 16px',
        display: 'flex', flexDirection: 'column', gap: 10 }}>
        {messages.map((m, i) => (
          <div key={i} style={{ maxWidth: '82%',
            alignSelf: m.role === 'user' ? 'flex-end' : 'flex-start',
            display: 'flex', flexDirection: 'column', gap: 6 }}>
            {m.role !== 'user' && m.role !== 'step' && m.agentName && (
              <div style={{ fontSize: 10, color: 'rgba(255,255,255,0.3)',
                paddingLeft: 4, marginBottom: -2 }}>{m.agentName}</div>
            )}
            {m.role === 'step' ? (
              <div style={{
                padding: '7px 12px', fontSize: 13, lineHeight: 1.5,
                borderRadius: 12,
                background: 'rgba(255,255,255,0.03)',
                color: 'rgba(255,255,255,0.45)',
                border: '1px solid rgba(255,255,255,0.06)',
                fontStyle: 'italic',
              }}>{m.text}</div>
            ) : (
            <div style={{
              padding: '10px 14px', fontSize: 14, lineHeight: 1.5,
              borderRadius: m.role === 'user' ? '18px 18px 4px 18px' : '4px 18px 18px 18px',
              background: m.role === 'user' ? LIME : 'rgba(255,255,255,0.06)',
              color: m.role === 'user' ? '#0B0B0E' : '#fff',
              border: m.role === 'user' ? 'none' : '1px solid rgba(255,255,255,0.07)',
            }}>{m.text}</div>
            )}
            {m.actions?.map((ac, ai) => (
              <div key={ai} style={{ display: 'flex', alignItems: 'center', gap: 8,
                padding: '7px 11px',
                background: 'rgba(200,255,62,0.06)',
                border: '1px solid rgba(200,255,62,0.2)', borderRadius: 12 }}>
                <div style={{ width: 22, height: 22, borderRadius: 7, background: LIME,
                  color: '#0B0B0E', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                  <Icon name="check" size={11} sw={2.5}/>
                </div>
                <span style={{ fontSize: 12, color: 'rgba(255,255,255,0.55)' }}>{ac.name}</span>
                <span style={{ fontSize: 12, color: LIME, fontFamily: 'monospace',
                  marginLeft: 'auto' }}>{ac.action}</span>
              </div>
            ))}
          </div>
        ))}

        {thinking && (
          <div style={{ alignSelf: 'flex-start', padding: '10px 14px',
            borderRadius: '4px 18px 18px 18px',
            background: 'rgba(255,255,255,0.06)',
            border: '1px solid rgba(255,255,255,0.07)',
            display: 'flex', flexDirection: 'column', gap: 6 }}>
            <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
              <span className="typing-dot"/>
              <span className="typing-dot" style={{ animationDelay: '.14s' }}/>
              <span className="typing-dot" style={{ animationDelay: '.28s' }}/>
            </div>
            {thinkingText && (
              <span style={{ fontSize: 11, color: 'rgba(255,255,255,0.32)',
                fontFamily: 'monospace' }}>{thinkingText}</span>
            )}
          </div>
        )}
        <div/>
      </div>

      {/* 扩展插槽（pendingRule 卡片、快捷建议等） */}
      {children}

      {/* 输入栏 */}
      <div style={{
        padding: '8px 12px',
        paddingBottom: variant === 'sheet'
          ? 'calc(12px + env(safe-area-inset-bottom, 0px))'
          : 'calc(8px + env(safe-area-inset-bottom, 0px))',
        borderTop: '1px solid rgba(255,255,255,0.06)',
        flexShrink: 0,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8,
          padding: '6px 6px 6px 16px',
          background: 'rgba(30,29,38,0.95)',
          border: '1px solid rgba(255,255,255,0.09)',
          borderRadius: 999 }}>
          <input
            ref={inputRef}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && !e.isComposing && handleSend()}
            placeholder={placeholder || '输入消息…'}
            disabled={disabled}
            style={{
              flex: 1, background: 'transparent', border: 'none',
              color: disabled ? 'rgba(255,255,255,0.25)' : '#fff',
              fontSize: 14, fontFamily: 'inherit', outline: 'none',
              WebkitTapHighlightColor: 'transparent',
            }}
          />
          <button
            onClick={handleSend}
            disabled={!input.trim() || disabled}
            style={{
              width: 38, height: 38, borderRadius: 999, flexShrink: 0,
              background: input.trim() && !disabled ? LIME : 'rgba(255,255,255,0.08)',
              color:      input.trim() && !disabled ? '#0B0B0E' : 'rgba(255,255,255,0.25)',
              border: 'none',
              cursor: input.trim() && !disabled ? 'pointer' : 'default',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              boxShadow: input.trim() && !disabled ? '0 0 18px rgba(200,255,62,0.35)' : 'none',
              WebkitTapHighlightColor: 'transparent',
            }}
          >
            <Icon name="arrow" size={16} sw={2.2}/>
          </button>
        </div>
      </div>
    </div>
  );
}

window.ChatPanel = ChatPanel;
