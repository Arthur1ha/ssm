/* ActivityFeed — 主屏活动流组件 */
function ButlerAvatar({ size = 36 }) {
  return (
    <div style={{
      width: size, height: size, borderRadius: '50%', flexShrink: 0,
      padding: 2,
      background: 'var(--color-surface-2)',
      border: '1px solid var(--color-accent-border)',
      boxShadow: '0 0 16px rgba(200,255,62,0.10)',
      overflow: 'hidden',
    }}>
      <img src="assets/butler-avatar.png" alt="管家" style={{
        width: '100%', height: '100%', objectFit: 'cover', borderRadius: '50%',
        display: 'block',
      }}/>
    </div>
  );
}

function ActivityFeed({ entries, thinking, thinkingText, onAdoptDevice, adoptingDeviceId }) {
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
          const agent = e.agent || 'orchestrator';
          const oc = getAgentBubbleColor(agent);
          const isButler = agent === 'orchestrator' || agent === 'cloud_orchestrator';
          return (
            <div key={i} style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: 8 }}>
              <div style={{ maxWidth: '88%', display: 'flex', gap: 9, alignItems: 'flex-start' }}>
                {isButler && <ButlerAvatar/>}
                <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
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
            </div>
          );
        }
        if (e.type === 'discovery') {
          const oc = getAgentBubbleColor('orchestrator');
          return (
            <div key={i} style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: 10 }}>
              <div style={{ maxWidth: '92%', display: 'flex', gap: 9, alignItems: 'flex-start' }}>
                <ButlerAvatar/>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 10, paddingLeft: 4, color: oc, opacity: 0.75,
                    fontFamily: 'var(--font-mono)', letterSpacing: '0.04em', marginBottom: 4 }}>
                    {getAgentDisplayName('orchestrator')}
                  </div>
                  <div style={{
                    padding: '10px 14px', fontSize: 14, lineHeight: 1.6,
                    borderRadius: 'var(--radius-card)',
                    background: `${oc}10`, border: `1px solid ${oc}25`,
                    color: 'var(--color-text)',
                  }}>{e.text}</div>
                  {(e.devices || []).map(candidate => (
                    <DiscoveryCandidateCard
                      key={candidate.device_id}
                      candidate={candidate}
                      onAdopt={onAdoptDevice}
                      adopting={adoptingDeviceId === candidate.device_id}
                    />
                  ))}
                </div>
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

      {thinking && (() => {
        const oc = getAgentBubbleColor('orchestrator');
        return (
        <div style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: 8 }}>
          <div style={{ display: 'flex', gap: 9, alignItems: 'flex-start' }}>
            <ButlerAvatar/>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <div style={{ fontSize: 10, paddingLeft: 4, color: oc, opacity: 0.75,
                fontFamily: 'var(--font-mono)', letterSpacing: '0.04em' }}>
                {getAgentDisplayName('orchestrator')}
              </div>
              <div style={{
                padding: '10px 14px', borderRadius: 'var(--radius-card)',
                background: `${oc}10`, border: `1px solid ${oc}25`,
                display: 'flex', flexDirection: 'column', gap: 6,
              }}>
                <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                  <span className="typing-dot" style={{ background: oc, opacity: 0.6 }}/>
                  <span className="typing-dot" style={{ background: oc, opacity: 0.6, animationDelay: '.14s' }}/>
                  <span className="typing-dot" style={{ background: oc, opacity: 0.6, animationDelay: '.28s' }}/>
                </div>
                {thinkingText && (
                  <span style={{ fontSize: 11, color: oc, opacity: 0.7,
                    fontFamily: 'var(--font-mono)' }}>
                  {thinkingText}
                </span>
                )}
              </div>
            </div>
          </div>
        </div>
        );
      })()}

      <div ref={bottomRef}/>
    </div>
  );
}

window.ActivityFeed = ActivityFeed;
