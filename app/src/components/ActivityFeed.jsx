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
      <img src="assets/butler-avatar.webp" alt="管家" style={{
        width: '100%', height: '100%', objectFit: 'cover', borderRadius: '50%',
        display: 'block',
      }}/>
    </div>
  );
}

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
        if (e.type === 'run_stage' || e.type === 'agent_progress') {
          const labels = { planning: '规划中', executing: '执行中', summarizing: '汇总中',
            finished: '已结束', dispatching: '已派发', observed: '已收到结果',
            ok: '已完成', accepted: '已受理', timeout: '结果未确认',
            rejected: '已拒绝', error: '执行失败' };
          return <div key={e.event_id || i} style={{ margin: '4px 16px 8px', padding: '6px 10px',
            borderLeft: '2px solid var(--color-border-strong)', color: 'var(--color-text-dim)',
            fontSize: e.type === 'run_stage' ? 12 : 11 }}>
            {e.type === 'agent_progress' && e.agent_id ? `${getAgentDisplayName(e.agent_id)} · ` : ''}
            {e.text || labels[e.stage] || labels[e.status] || '状态已更新'}
          </div>;
        }
        if (e.type === 'trace') {
          return <div key={i} style={{ textAlign: 'center', marginBottom: 8 }}>
            <button type="button" onClick={() => navigator.clipboard?.writeText(e.traceId)}
              title="复制任务编号" style={{ fontSize: 10, color: 'var(--color-text-dim)',
                fontFamily: 'var(--font-mono)', background: 'var(--color-surface-2)',
                border: '1px solid var(--color-border)', borderRadius: 999,
                padding: '4px 10px', cursor: 'pointer' }}>
              任务编号 {e.traceId} · 点击复制
            </button>
          </div>;
        }
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

      {thinking && <div role="status" style={{ margin: '6px 16px 10px',
        fontSize: 11, color: 'var(--color-text-dim)' }}>
        {thinkingText || '正在处理'}
      </div>}

      <div ref={bottomRef}/>
    </div>
  );
}

window.ActivityFeed = ActivityFeed;
