function SensorCard({ agent, unitData }) {
  const uid     = agent.unit_id || agent.agent_id;
  const meta    = getAgentMeta(agent);
  const reading = getSensorReading(agent, unitData);
  const offline = !agent._online;
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 12, padding: '12px 14px',
      marginBottom: 8, borderRadius: 'var(--radius-card)',
      background: 'var(--color-surface-1)',
      border: '1px solid var(--color-border)',
      opacity: offline ? 0.4 : 1,
    }}>
      <div style={{ width: 42, height: 42, borderRadius: 13, flexShrink: 0,
        background: `${meta.color}18`, color: meta.color,
        display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Icon name={meta.icon} size={19}/>
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 14, fontWeight: 500, overflow: 'hidden',
          textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--color-text)' }}>
          {agent.name || uid}
        </div>
        <div style={{ fontSize: 11, color: 'var(--color-text-dim)',
          fontFamily: 'var(--font-mono)', marginTop: 2 }}>
          {offline ? '离线' : `${meta.label} · 只读`}
        </div>
      </div>
      <span style={{ fontSize: 14, fontWeight: 600, color: reading.color,
        fontFamily: 'var(--font-mono)', flexShrink: 0 }}>
        {reading.value}
      </span>
      <div style={{ width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
        background: offline ? 'var(--color-offline)' : 'var(--color-online)',
        boxShadow: offline ? 'none' : '0 0 6px var(--color-online-glow)' }}/>
    </div>
  );
}

function ActuatorCard({ agent, unitData }) {
  const uid    = agent.unit_id || agent.agent_id;
  const meta   = getAgentMeta(agent);
  const active = isAgentActive(agent, unitData);
  const offline = !agent._online;

  return (
    <div
      onClick={offline ? undefined : () => navigate('#/devices/' + (agent.slug || uid))}
      className={offline ? '' : 'interactive'}
      style={{
        padding: '12px 14px', marginBottom: 10,
        borderRadius: 'var(--radius-card)',
        background: active && !offline
          ? `linear-gradient(135deg, ${meta.color}15, var(--color-surface-1))`
          : 'var(--color-surface-1)',
        border: `1px solid ${active && !offline ? meta.color + '40' : 'var(--color-border)'}`,
        opacity: offline ? 0.4 : 1,
        cursor: offline ? 'default' : 'pointer',
      }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <div style={{ width: 42, height: 42, borderRadius: 13, flexShrink: 0,
          background: active && !offline ? meta.color : 'var(--color-surface-2)',
          color: active && !offline ? 'var(--color-bg)' : meta.color,
          display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <Icon name={meta.icon} size={19}/>
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 500, overflow: 'hidden',
            textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--color-text)' }}>
            {agent.name || uid}
          </div>
          <div style={{ fontSize: 11, marginTop: 2, fontFamily: 'var(--font-mono)',
            color: offline ? 'var(--color-text-dim)' : (active ? meta.color : 'var(--color-text-dim)') }}>
            {offline ? '离线' : getStateLabel(agent, unitData)}
          </div>
        </div>
        {!offline && (
          <div style={{ flexShrink: 0, color: 'var(--color-text-dim)' }}>
            <Icon name="arrow" size={13} sw={2}/>
          </div>
        )}
        <div style={{ width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
          background: offline ? 'var(--color-offline)' : 'var(--color-online)',
          boxShadow: offline ? 'none' : '0 0 6px var(--color-online-glow)' }}/>
      </div>
    </div>
  );
}

window.SensorCard  = SensorCard;
window.ActuatorCard = ActuatorCard;
