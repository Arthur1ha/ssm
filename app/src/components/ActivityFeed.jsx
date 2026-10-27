/* ActivityFeed — 主屏活动流组件 */
function ActivityFeed({ entries }) {
  const { useRef, useEffect } = React;
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [entries]);

  if (entries.length === 0) return null;
  const latestRequestId = [...entries].reverse().find(e => e.type === 'ai' && e.requestId)?.requestId;

  return (
    <div style={{ padding: '0 16px' }}>
      {entries.map((e, i) => {
        if (e.type === 'run_stage' || e.type === 'agent_progress') {
          return <div key={e.event_id || i} style={{ margin: '4px 16px 8px', padding: '6px 10px',
            borderLeft: '2px solid var(--color-border-strong)', color: 'var(--color-text-dim)',
            fontSize: ['planning', 'executing', 'summarizing', 'finished'].includes(e.stage) ? 12 : 11 }}>
            {e.type === 'agent_progress' && e.agent_id ? `${getAgentDisplayName(e.agent_id)} · ` : ''}
            {e.stage === 'narrated' && e.text ? e.text : describeRunProgress(e)}
          </div>;
        }
        if (e.type === 'user') {
          return (
            <div key={i} style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 8 }}>
              <div style={{ maxWidth: '78%', display: 'flex', flexDirection: 'column',
                alignItems: 'flex-end', gap: 4 }}>
                <div style={{ fontSize: 10, paddingRight: 4, color: 'var(--color-accent)',
                  opacity: 0.75, fontFamily: 'var(--font-mono)', letterSpacing: '0.04em' }}>你</div>
                <div style={{
                  padding: '10px 14px', fontSize: 14, lineHeight: 1.5,
                  borderRadius: 'var(--radius-card)',
                  background: 'var(--color-accent)', color: 'var(--color-bg)',
                }}>{e.text}</div>
              </div>
            </div>
          );
        }
        if (e.type === 'ai') {
          const agent = e.agent || 'orchestrator';
          const oc = getAgentBubbleColor(agent);
          return (
            <div key={e.requestId || i} style={{ display: 'flex', justifyContent: 'flex-start',
              marginBottom: 8, position: e.requestId && e.requestId === latestRequestId ? 'sticky' : 'relative',
              top: e.requestId && e.requestId === latestRequestId ? 8 : undefined,
              zIndex: e.requestId && e.requestId === latestRequestId ? 2 : undefined }}>
              <div style={{ maxWidth: '88%', minWidth: 0, display: 'flex',
                flexDirection: 'column', gap: 4 }}>
                <div style={{ fontSize: 10, paddingLeft: 4, color: oc, opacity: 0.75,
                  fontFamily: 'var(--font-mono)', letterSpacing: '0.04em' }}>
                  {getAgentDisplayName(agent)}
                </div>
                <div style={{
                  padding: '10px 14px', fontSize: 14, lineHeight: 1.6,
                  borderRadius: 'var(--radius-card)',
                  background: `${oc}10`,
                  border: `1px solid ${oc}25`,
                  color: 'var(--color-text)',
                }}>{e.text}</div>
              </div>
            </div>
          );
        }
        if (e.type === 'event') {
          /* 设备状态变化：居中的低存在感小药丸，明显区别于左右对话气泡，不抢戏 */
          return (
            <div key={i} style={{ display: 'flex', justifyContent: 'center', marginBottom: 8 }}>
              <span style={{
                fontSize: 10.5, color: 'var(--color-text-dim)',
                fontFamily: 'var(--font-mono)', letterSpacing: '0.02em',
                background: 'var(--color-surface-2)', border: '1px solid var(--color-border)',
                borderRadius: 999, padding: '3px 10px', opacity: 0.85,
              }}>
                {e.text}
                <span style={{ marginLeft: 6, opacity: 0.6 }}>
                  {new Date(e.ts).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}
                </span>
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

      <div ref={bottomRef}/>
    </div>
  );
}

window.ActivityFeed = ActivityFeed;
