function MainInputBar({ onSend, thinking, leadingAction, placeholder }) {
  const [input, setInput] = useState('');

  const handleSend = () => {
    const t = input.trim();
    if (!t || thinking) return;
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
        {leadingAction && (
          <button
            onClick={leadingAction.onClick}
            title={leadingAction.label}
            disabled={thinking}
            style={{
              width: 34, height: 34, borderRadius: 999, flexShrink: 0,
              background: 'var(--color-surface-2)',
              color: 'var(--color-text-muted)',
              border: '1px solid var(--color-border)',
              cursor: thinking ? 'default' : 'pointer',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              WebkitTapHighlightColor: 'transparent',
            }}
          >
            <Icon name={leadingAction.icon || 'scan'} size={15} sw={2}/>
          </button>
        )}
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && !e.isComposing && handleSend()}
          placeholder={thinking ? '处理中…' : (placeholder || '告诉我想要什么…')}
          disabled={thinking}
          style={{
            flex: 1, background: 'transparent', border: 'none',
            color: thinking ? 'rgba(255,255,255,0.35)' : '#fff',
            fontSize: 14, fontFamily: 'inherit', outline: 'none',
            WebkitTapHighlightColor: 'transparent',
          }}
        />
        <button
          onClick={handleSend}
          disabled={!input.trim() || thinking}
          style={{
            width: 38, height: 38, borderRadius: 999, flexShrink: 0,
            background: input.trim() && !thinking ? 'var(--color-accent)' : 'var(--color-surface-2)',
            color:      input.trim() && !thinking ? 'var(--color-bg)' : 'var(--color-text-dim)',
            border: 'none',
            cursor: input.trim() && !thinking ? 'pointer' : 'default',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            boxShadow: input.trim() && !thinking ? '0 0 18px var(--color-accent-glow)' : 'none',
            WebkitTapHighlightColor: 'transparent',
          }}
        >
          <Icon name="arrow" size={16} sw={2.2}/>
        </button>
      </div>
    </div>
  );
}
