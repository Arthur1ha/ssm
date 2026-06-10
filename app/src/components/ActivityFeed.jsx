/* ActivityFeed — 主屏活动流组件 */
function ActivityFeed({ entries, thinking, thinkingText }) {
  const { useRef, useEffect } = React;
  const LIME = '#C8FF3E';
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
                borderRadius: '18px 18px 4px 18px',
                background: LIME, color: '#0B0B0E',
              }}>{e.text}</div>
            </div>
          );
        }
        if (e.type === 'ai') {
          return (
            <div key={i} style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: 8 }}>
              <div style={{
                maxWidth: '78%', padding: '10px 14px', fontSize: 14, lineHeight: 1.5,
                borderRadius: '4px 18px 18px 18px',
                background: 'rgba(255,255,255,0.06)',
                border: '1px solid rgba(255,255,255,0.07)', color: '#fff',
              }}>{e.text}</div>
            </div>
          );
        }
        if (e.type === 'event') {
          return (
            <div key={i} style={{ marginBottom: 6, padding: '0 4px' }}>
              <span style={{
                fontSize: 11, color: 'rgba(255,255,255,0.3)',
                fontFamily: 'monospace',
              }}>
                {new Date(e.ts).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}
                {'  '}{e.text}
              </span>
            </div>
          );
        }
        return null;
      })}

      {thinking && (
        <div style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: 8 }}>
          <div style={{
            padding: '10px 14px', borderRadius: '4px 18px 18px 18px',
            background: 'rgba(255,255,255,0.06)',
            border: '1px solid rgba(255,255,255,0.07)',
            display: 'flex', flexDirection: 'column', gap: 6,
          }}>
            <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
              <span className="typing-dot"/>
              <span className="typing-dot" style={{ animationDelay: '.14s' }}/>
              <span className="typing-dot" style={{ animationDelay: '.28s' }}/>
            </div>
            {thinkingText && (
              <span style={{ fontSize: 11, color: 'rgba(255,255,255,0.32)', fontFamily: 'monospace' }}>
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
