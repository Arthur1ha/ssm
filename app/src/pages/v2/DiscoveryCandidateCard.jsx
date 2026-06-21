function DiscoveryCandidateCard({ candidate, onAdopt, adopting }) {
  const deviceId = candidate.device_id;
  const unitCount = candidate.unit_ids?.length || candidate.cards?.length || 0;
  const skills = (candidate.skills || []).filter(Boolean).slice(0, 4);
  const sensors = (candidate.sensors || []).filter(Boolean).slice(0, 3);
  const transports = (candidate.transport_kinds || []).filter(Boolean);
  const online = candidate.online !== false;
  const buttonText = !online ? '设备离线' : (adopting ? '接入中...' : '接入');

  const chip = (text, tone = 'normal') => (
    <span key={tone + text} style={{
      fontSize: 10.5,
      color: tone === 'accent' ? 'var(--color-accent)' : 'var(--color-text-muted)',
      border: `1px solid ${tone === 'accent' ? 'var(--color-accent-border)' : 'var(--color-border)'}`,
      background: tone === 'accent' ? 'var(--color-accent-dim)' : 'var(--color-surface-2)',
      borderRadius: 'var(--radius-pill)',
      padding: '3px 8px',
      whiteSpace: 'nowrap',
    }}>{text}</span>
  );

  return (
    <div className="glass" style={{
      background: 'var(--color-surface-1)',
      border: '1px solid var(--color-border)',
      borderRadius: 'var(--radius-card)',
      padding: 12,
      marginTop: 8,
    }}>
      <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
        <div style={{
          width: 38, height: 38, borderRadius: 'var(--radius-btn)',
          background: online ? 'var(--color-accent-dim)' : 'var(--color-surface-2)',
          color: online ? 'var(--color-accent)' : 'var(--color-text-dim)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          flexShrink: 0,
        }}>
          <Icon name={candidate.agent_type === 'robot' ? 'zap' : 'wifi'} size={18}/>
        </div>

        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
            <div style={{
              color: 'var(--color-text)', fontSize: 14, fontWeight: 600,
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}>
              {candidate.name || deviceId}
            </div>
            <span style={{
              width: 7, height: 7, borderRadius: '50%', flexShrink: 0,
              background: online ? 'var(--color-online)' : 'var(--color-offline)',
              boxShadow: online ? '0 0 6px var(--color-online-glow)' : 'none',
            }}/>
          </div>

          <div style={{
            color: 'var(--color-text-dim)', fontSize: 12, lineHeight: 1.5,
            marginBottom: 9,
          }}>
            {candidate.summary || '已发布能力声明'}
          </div>

          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 10 }}>
            {unitCount > 0 && chip(`${unitCount} 个 unit`, 'accent')}
            {transports.map(t => chip(t.toUpperCase()))}
            {skills.map(s => chip(s))}
            {sensors.map(s => chip(s))}
          </div>

          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn" onClick={() => onAdopt?.(candidate)}
              disabled={adopting || !online}
              style={{
                flex: 1,
                justifyContent: 'center',
                background: online ? 'var(--color-accent)' : 'var(--color-surface-2)',
                color: online ? 'var(--color-bg)' : 'var(--color-text-dim)',
                borderColor: online ? 'var(--color-accent-border)' : 'var(--color-border)',
                opacity: adopting ? 0.7 : 1,
                cursor: adopting || !online ? 'default' : 'pointer',
              }}>
              {online && <Icon name="arrow" size={13}/>}
              {buttonText}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

window.DiscoveryCandidateCard = DiscoveryCandidateCard;
