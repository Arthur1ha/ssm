/* ActivityFeed — 主屏活动流组件 */
function ActivityFeed({ entries, thinking, thinkingText }) {
  const { useRef, useEffect } = React;
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [entries, thinking]);

  if (entries.length === 0 && !thinking) return null;

  return (
    <div style={{ padding: '0 16px' }}>
      {entries.map((e, i) => {
        if (e.type === 'user') {
          return (
            <div key={i} style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 8 }}>
              <div style={{
                maxWidth: '78%', padding: '10px 14px', fontSize: 14, lineHeight: 1.5,
                borderRadius: 'var(--radius-card)',
                background: 'var(--color-accent)', color: 'var(--color-bg)',
              }}>{e.text}</div>
            </div>
          );
        }
        if (e.type === 'ai') {
          return (
            <div key={i} style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: 8 }}>
              <div style={{
                maxWidth: '78%', padding: '10px 14px', fontSize: 14, lineHeight: 1.5,
                borderRadius: 'var(--radius-card)',
                background: 'var(--color-surface-2)',
                border: '1px solid var(--color-border)', color: 'var(--color-text)',
              }}>{e.text}</div>
            </div>
          );
        }
        if (e.type === 'event') {
          return (
            <div key={i} style={{ marginBottom: 6, padding: '0 4px' }}>
              <span style={{
                fontSize: 11, color: 'var(--color-text-dim)',
                fontFamily: 'var(--font-mono)',
              }}>
                {new Date(e.ts).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}
                {'  '}{e.text}
              </span>
            </div>
          );
        }
        if (e.type === 'system') {
          return (
            <div key={i} style={{ marginBottom: 8, display: 'flex', alignItems: 'center', gap: 8 }}>
              <div style={{ flex: 1, height: 1, background: 'var(--color-border)' }}/>
              <span style={{
                fontSize: 11, color: 'var(--color-text-dim)',
                fontFamily: 'var(--font-mono)', whiteSpace: 'nowrap',
              }}>{e.text}</span>
              <div style={{ flex: 1, height: 1, background: 'var(--color-border)' }}/>
            </div>
          );
        }
        return null;
      })}

      {thinking && (
        <div style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: 8 }}>
          <div style={{
            padding: '10px 14px', borderRadius: 'var(--radius-card)',
            background: 'var(--color-surface-2)',
            border: '1px solid var(--color-border)',
            display: 'flex', flexDirection: 'column', gap: 6,
          }}>
            <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
              <span className="typing-dot"/>
              <span className="typing-dot" style={{ animationDelay: '.14s' }}/>
              <span className="typing-dot" style={{ animationDelay: '.28s' }}/>
            </div>
            {thinkingText && (
              <span style={{ fontSize: 11, color: 'var(--color-text-dim)',
                fontFamily: 'var(--font-mono)' }}>
                {thinkingText}
              </span>
            )}
          </div>
        </div>
      )}

      <div ref={bottomRef}/>
    </div>
  );
}

window.ActivityFeed = ActivityFeed;
