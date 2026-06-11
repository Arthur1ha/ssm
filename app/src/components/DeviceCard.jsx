function SensorCard({ agent, unitData }) {
  const uid     = agent.unit_id || agent.agent_id;
  const meta    = getAgentMeta(agent);
  const reading = getSensorReading(agent, unitData);
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 12, padding: '12px 14px',
      marginBottom: 8, borderRadius: 18,
      background: 'rgba(255,255,255,0.03)',
      border: '1px solid rgba(255,255,255,0.05)',
    }}>
      <div style={{ width: 42, height: 42, borderRadius: 13, flexShrink: 0,
        background: `${meta.color}18`, color: meta.color,
        display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Icon name={meta.icon} size={19}/>
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 14, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {agent.name || uid}
        </div>
        <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.28)', fontFamily: 'monospace', marginTop: 2 }}>
          {meta.label} · 只读
        </div>
      </div>
      <span style={{ fontSize: 14, fontWeight: 600, color: reading.color, fontFamily: 'monospace', flexShrink: 0 }}>
        {reading.value}
      </span>
      <div style={{ width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
        background: agent._online ? LIME : 'rgba(255,255,255,0.2)',
        boxShadow: agent._online ? `0 0 6px ${LIME}` : 'none' }}/>
    </div>
  );
}

function ActuatorCard({ agent, unitData }) {
  const uid    = agent.unit_id || agent.agent_id;
  const meta   = getAgentMeta(agent);
  const active = isAgentActive(agent, unitData);

  return (
    <div
      onClick={() => navigate('#/devices/' + (agent.slug || uid))}
      style={{
        padding: '12px 14px', marginBottom: 10, borderRadius: 18,
        background: active
          ? `linear-gradient(135deg, ${meta.color}15, rgba(255,255,255,0.03))`
          : 'rgba(255,255,255,0.04)',
        border: `1px solid ${active ? meta.color + '40' : 'rgba(255,255,255,0.07)'}`,
        cursor: 'pointer',
      }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <div style={{ width: 42, height: 42, borderRadius: 13, flexShrink: 0,
          background: active ? meta.color : 'rgba(255,255,255,0.06)',
          color: active ? '#0B0B0E' : meta.color,
          display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <Icon name={meta.icon} size={19}/>
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {agent.name || uid}
          </div>
          <div style={{ fontSize: 11, color: active ? meta.color : 'rgba(255,255,255,0.3)', marginTop: 2, fontFamily: 'monospace' }}>
            {getStateLabel(agent, unitData)}
          </div>
        </div>
        <div style={{ flexShrink: 0, color: 'rgba(255,255,255,0.3)' }}>
          <Icon name="arrow" size={13} sw={2}/>
        </div>
        <div style={{ width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
          background: agent._online ? LIME : 'rgba(255,255,255,0.2)',
          boxShadow: agent._online ? `0 0 6px ${LIME}` : 'none' }}/>
      </div>
    </div>
  );
}

window.SensorCard  = SensorCard;
window.ActuatorCard = ActuatorCard;
