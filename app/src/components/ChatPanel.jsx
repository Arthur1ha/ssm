/* ChatPanel — 统一聊天面板组件（variant="inline"：内嵌页面，撑满父容器剩余高度） */
const { useState, useEffect, useRef } = React;

function ChatPanel({ messages, thinking, thinkingText, thinkingAgent, thinkingAgentName,
                     onSend, placeholder, variant, disabled, open, children }) {
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
        {messages.map((m, i) => {
          const agentColor = getAgentBubbleColor(m.agent);
          return (
          <div key={i} style={{ maxWidth: '82%',
            alignSelf: m.role === 'user' ? 'flex-end' : 'flex-start',
            display: 'flex', flexDirection: 'column', gap: 4 }}>
            {m.role !== 'user' && m.role !== 'step' && (
              <div style={{
                fontSize: 10, paddingLeft: 4,
                color: agentColor, opacity: 0.75,
                fontFamily: 'var(--font-mono)', letterSpacing: '0.04em',
              }}>
                {m.agentName || getAgentDisplayName(m.agent)}
              </div>
            )}
            {m.role === 'step' ? (
              <div style={{
                padding: '6px 12px', fontSize: 12, lineHeight: 1.5,
                borderRadius: 8,
                background: `${agentColor}08`,
                color: agentColor,
                border: `1px solid ${agentColor}20`,
                fontFamily: 'var(--font-mono)', opacity: 0.7,
              }}>{m.text}</div>
            ) : (
            <div style={{
              padding: '10px 14px', fontSize: 14, lineHeight: 1.6,
              borderRadius: m.role === 'user' ? '16px 16px 4px 16px' : '4px 16px 16px 16px',
              background: m.role === 'user' ? 'var(--color-accent)' : `${agentColor}10`,
              color: m.role === 'user' ? 'var(--color-bg)' : 'var(--color-text)',
              border: m.role === 'user' ? 'none' : `1px solid ${agentColor}25`,
            }}>{m.text}</div>
            )}
            {m.actions?.map((ac, ai) => (
              <div key={ai} style={{ display: 'flex', alignItems: 'center', gap: 8,
                padding: '7px 11px',
                background: 'var(--color-accent-dim)',
                border: '1px solid rgba(200,255,62,0.2)', borderRadius: 12 }}>
                <div style={{ width: 22, height: 22, borderRadius: 7, background: 'var(--color-accent)',
                  color: 'var(--color-bg)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                  <Icon name="check" size={11} sw={2.5}/>
                </div>
                <span style={{ fontSize: 12, color: 'rgba(255,255,255,0.55)' }}>{ac.name}</span>
                <span style={{ fontSize: 12, color: 'var(--color-accent)', fontFamily: 'monospace',
                  marginLeft: 'auto' }}>{ac.action}</span>
              </div>
            ))}
          </div>
          );
        })}

        {thinking && (() => {
          const tc = getAgentBubbleColor(thinkingAgent);
          return (
            <div style={{ alignSelf: 'flex-start', display: 'flex', flexDirection: 'column', gap: 4 }}>
              <div style={{ fontSize: 10, paddingLeft: 4, color: tc, opacity: 0.75,
                fontFamily: 'var(--font-mono)', letterSpacing: '0.04em' }}>
                {thinkingAgentName || getAgentDisplayName(thinkingAgent)}
              </div>
              <div style={{ padding: '10px 14px',
                borderRadius: '4px 16px 16px 16px',
                background: `${tc}10`, border: `1px solid ${tc}25`,
                display: 'flex', flexDirection: 'column', gap: 6 }}>
                <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                  <span className="typing-dot" style={{ background: tc, opacity: 0.6 }}/>
                  <span className="typing-dot" style={{ background: tc, opacity: 0.6, animationDelay: '.14s' }}/>
                  <span className="typing-dot" style={{ background: tc, opacity: 0.6, animationDelay: '.28s' }}/>
                </div>
                {thinkingText && (
                  <span style={{ fontSize: 11, color: tc, opacity: 0.7,
                    fontFamily: 'var(--font-mono)' }}>{thinkingText}</span>
                )}
              </div>
            </div>
          );
        })()}
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
        borderTop: '1px solid var(--color-border)',
        flexShrink: 0,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8,
          padding: '6px 6px 6px 16px',
          background: 'rgba(30,29,38,0.95)',
          border: '1px solid var(--color-border)',
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
              color: disabled ? 'var(--color-text-dim)' : 'var(--color-text)',
              fontSize: 14, fontFamily: 'inherit', outline: 'none',
              WebkitTapHighlightColor: 'transparent',
            }}
          />
          <button
            onClick={handleSend}
            disabled={!input.trim() || disabled}
            style={{
              width: 38, height: 38, borderRadius: 999, flexShrink: 0,
              background: input.trim() && !disabled ? 'var(--color-accent)' : 'var(--color-surface-2)',
              color:      input.trim() && !disabled ? 'var(--color-bg)' : 'var(--color-text-dim)',
              border: 'none',
              cursor: input.trim() && !disabled ? 'pointer' : 'default',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              boxShadow: input.trim() && !disabled ? '0 0 18px var(--color-accent-glow)' : 'none',
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
