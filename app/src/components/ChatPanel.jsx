/* Shared panel shell. Pages supply context and domain-specific feed cards;
 * ChatMessages and ChatInput own all conversation presentation. */
function ChatPanel({ messages = [], agents, defaultAgent, training, onReviseRule,
  onSend, busy, disabled, placeholder, focusKey, suggestions = [],
  beforeMessages, messageContent, children }) {
  return <div className="chat-panel">
    <div className="chat-panel__scroll">
      {beforeMessages}
      {messageContent || <ChatMessages entries={messages} agents={agents} defaultAgent={defaultAgent}
        training={training} onReviseRule={onReviseRule}/>}
      <div style={{ height: 16 }}/>
    </div>
    <div className="chat-panel__footer">
      {suggestions.length > 0 && <div className="chat-suggestions">
        {suggestions.map(text => <button type="button" key={text}
          disabled={disabled || busy} onClick={() => onSend(text)}>{text}</button>)}
      </div>}
      {children}
      <ChatInput onSend={onSend} busy={busy} disabled={disabled}
        placeholder={placeholder} focusKey={focusKey}/>
    </div>
  </div>;
}
window.ChatPanel = ChatPanel;

function ChatInput({ onSend, busy, disabled, placeholder, focusKey }) {
  const inputRef = React.useRef(null);
  React.useEffect(() => { if (focusKey) inputRef.current?.focus(); }, [focusKey]);
  const unavailable = disabled || busy;
  const [input, setInput] = React.useState('');

  const handleSend = () => {
    const t = input.trim();
    if (!t || unavailable) return;
    setInput('');
    onSend(t);
  };

  return (
    <div style={{
      flexShrink: 0, padding: '8px 12px',
      paddingBottom: 'calc(8px + env(safe-area-inset-bottom, 0px))',
      background: 'transparent',
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '6px 6px 6px 16px',
        background: 'rgba(30,29,38,0.95)',
        border: '1px solid var(--color-border)',
        borderRadius: 999,
      }}>
        <input
          ref={inputRef}
          aria-label="聊天消息"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && !e.nativeEvent.isComposing && !e.isComposing && handleSend()}
          placeholder={busy && !disabled ? '处理中…' : (placeholder || '告诉我想要什么…')}
          disabled={unavailable}
          style={{
            flex: 1, minWidth: 0, background: 'transparent', border: 'none',
            color: unavailable ? 'rgba(255,255,255,0.35)' : '#fff',
            fontSize: 14, fontFamily: 'inherit', outline: 'none',
            WebkitTapHighlightColor: 'transparent',
          }}
        />
        <button
          type="button"
          aria-label="发送消息"
          onClick={handleSend}
          disabled={!input.trim() || unavailable}
          style={{
            width: 38, height: 38, borderRadius: 999, flexShrink: 0,
            background: input.trim() && !unavailable ? 'var(--color-accent)' : 'var(--color-surface-2)',
            color:      input.trim() && !unavailable ? 'var(--color-bg)' : 'var(--color-text-dim)',
            border: 'none',
            cursor: input.trim() && !unavailable ? 'pointer' : 'default',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            boxShadow: input.trim() && !unavailable ? '0 0 18px var(--color-accent-glow)' : 'none',
            WebkitTapHighlightColor: 'transparent',
          }}
        >
          <Icon name="arrow" size={16} sw={2.2}/>
        </button>
      </div>
    </div>
  );
}
