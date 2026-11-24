/* Shared panel shell. Pages supply context and domain-specific feed cards;
 * ChatMessages and ChatInput own all conversation presentation. */
function ChatPanel({ messages = [], agents, defaultAgent, training,
  onSend, onLocate, busy, busyProgress, disabled, placeholder, focusKey, suggestions = [],
  beforeMessages, messageContent, inputAvatar, children }) {
  const scrollRef = React.useRef(null);
  const contentRef = React.useRef(null);
  const following = React.useRef(true);
  const resumeOnBottom = React.useRef(false);
  const touchY = React.useRef(null);
  const [paused,setPaused] = React.useState(false);
  const followLatest = React.useCallback(() => {
    following.current=true;
    resumeOnBottom.current=false;
    setPaused(false);
    const scroller=scrollRef.current;
    if (scroller) scroller.scrollTop=scroller.scrollHeight;
  },[]);
  React.useLayoutEffect(() => {
    if (following.current) followLatest();
  });
  React.useEffect(() => {
    const observer=typeof ResizeObserver==='function' ? new ResizeObserver(() => {
      if (following.current) followLatest();
    }) : null;
    if (contentRef.current) observer?.observe(contentRef.current);
    if (scrollRef.current) observer?.observe(scrollRef.current);
    return () => observer?.disconnect();
  },[followLatest]);
  const pauseFollowing = () => {
    following.current=false;
    resumeOnBottom.current=false;
    setPaused(true);
  };
  const onScroll = event => {
    const scroller=event.currentTarget;
    // Layout, scroll anchoring and our own writes also emit scroll events.
    // Only an explicit upward input pauses following, never a changed offset.
    if (following.current || resumeOnBottom.current && scroller.scrollHeight-scroller.clientHeight-scroller.scrollTop<=2) followLatest();
  };
  const onTouchMove = event => {
    const current=event.touches[0]?.clientY;
    if (current != null && touchY.current != null && current>touchY.current+2) pauseFollowing();
    else if (current != null && touchY.current != null && current<touchY.current-2) resumeOnBottom.current=true;
    touchY.current=current ?? null;
  };
  const onKeyDown = event => {
    if (event.defaultPrevented || event.target.closest('input,textarea,select,[contenteditable="true"]')) return;
    if (['ArrowUp','PageUp','Home'].includes(event.key) || event.key===' ' && event.shiftKey) pauseFollowing();
    else if (['ArrowDown','PageDown','End',' '].includes(event.key)) resumeOnBottom.current=true;
  };
  return <div className="chat-panel" aria-busy={Boolean(busy)}>
    <div className="chat-panel__scroll" ref={scrollRef} tabIndex={0} aria-label="聊天记录" onScroll={onScroll}
      onWheel={event => {if (event.deltaY<0) pauseFollowing();else if (event.deltaY>0) resumeOnBottom.current=true;}}
      onTouchStart={event => {touchY.current=event.touches[0]?.clientY ?? null;}} onTouchMove={onTouchMove}
      onTouchEnd={() => {touchY.current=null;}} onTouchCancel={() => {touchY.current=null;}} onKeyDown={onKeyDown}>
      <div ref={contentRef} className="chat-panel__content">
      {beforeMessages}
      {messageContent || <ChatMessages entries={messages} agents={agents} defaultAgent={defaultAgent}
        training={training} onLocate={onLocate} busy={busy && !disabled} busyProgress={busyProgress}/>}
      <div style={{ height: 16 }}/>
      </div>
    </div>
    <div className="chat-panel__footer">
      {paused && <button type="button" className="chat-panel__latest" onClick={followLatest}>回到最新 ↓</button>}
      {suggestions.length > 0 && <div className="chat-suggestions">
        {suggestions.map(text => <button type="button" key={text}
          disabled={disabled || busy} onClick={() => onSend(text)}>{text}</button>)}
      </div>}
      {children}
      <ChatInput onSend={onSend} busy={busy} disabled={disabled}
        placeholder={placeholder} focusKey={focusKey} avatar={inputAvatar}/>
    </div>
  </div>;
}
window.ChatPanel = ChatPanel;

function ChatInput({ onSend, busy, disabled, placeholder, focusKey, avatar }) {
  const inputRef = React.useRef(null);
  React.useEffect(() => { if (focusKey) inputRef.current?.focus(); }, [focusKey]);
  const unavailable = disabled;
  const [input, setInput] = React.useState('');

  const handleSend = () => {
    const t = input.trim();
    if (!t || unavailable) return;
    setInput('');
    onSend(t);
  };

  return (
    <div className={`chat-input${avatar ? ' chat-input--companion' : ''}`} style={{
      flexShrink: 0, padding: '8px 12px',
      paddingBottom: 'calc(8px + env(safe-area-inset-bottom, 0px))',
      background: 'transparent',
    }}>
      {avatar && <span className="chat-input__avatar">{avatar}</span>}
      <div className="chat-input__field" style={{
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
          placeholder={busy && !disabled ? '输入新的要求…' : (placeholder || '告诉我想要什么…')}
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
