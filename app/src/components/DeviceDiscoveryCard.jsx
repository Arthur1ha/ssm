/* DeviceDiscoveryCard — GPS 附近弹出的设备发现卡片 */
function DeviceDiscoveryCard({ agent, unitData, onDismiss, onGo }) {
  const meta    = getAgentMeta(agent);
  const reading = agent.agent_type !== 'actuator'
    ? getSensorReading(agent, unitData)
    : { value: getStateLabel(agent, unitData), color: meta.color };

  return (
    <div style={{
      position: 'absolute', left: 0, right: 0, bottom: 0, zIndex: 50,
      background: 'rgba(14,13,20,0.97)',
      backdropFilter: 'blur(32px) saturate(150%)',
      borderTop: '1px solid rgba(255,255,255,0.1)',
      borderRadius: '24px 24px 0 0',
      padding: '16px 20px calc(20px + env(safe-area-inset-bottom, 0px))',
    }}>
      <div style={{ width: 36, height: 4, borderRadius: 2, background: 'rgba(255,255,255,0.15)', margin: '0 auto 18px' }}/>
      <div style={{ fontSize: 10, color: 'rgba(255,255,255,0.3)', textAlign: 'center',
        fontFamily: 'monospace', letterSpacing: '0.1em', marginBottom: 16 }}>附近发现设备</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginBottom: 22 }}>
        <div style={{ width: 52, height: 52, borderRadius: 16, flexShrink: 0,
          background: `${meta.color}18`, color: meta.color,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          border: `1px solid ${meta.color}30` }}>
          <Icon name={meta.icon} size={24}/>
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 16, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {agent.name || agent.unit_id}
          </div>
          <div style={{ fontSize: 12, color: 'rgba(255,255,255,0.38)', marginTop: 3 }}>
            {meta.label} · {agent.agent_type === 'actuator' ? '执行器' : '传感器'}
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: reading.color }}>{reading.value}</span>
          <div style={{ width: 8, height: 8, borderRadius: '50%',
            background: agent._online ? LIME : 'rgba(255,255,255,0.2)',
            boxShadow: agent._online ? `0 0 6px ${LIME}` : 'none' }}/>
        </div>
      </div>
      <div style={{ display: 'flex', gap: 10 }}>
        <button onClick={onDismiss} style={{
          flex: 1, padding: '13px 0', borderRadius: 14, fontSize: 14, fontWeight: 500,
          background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.1)',
          color: 'rgba(255,255,255,0.55)', cursor: 'pointer', fontFamily: 'inherit',
        }}>稍后</button>
        <button onClick={onGo} style={{
          flex: 2, padding: '13px 0', borderRadius: 14, fontSize: 14, fontWeight: 600,
          background: LIME, border: 'none', color: '#0B0B0E', cursor: 'pointer', fontFamily: 'inherit',
        }}>前往设备 →</button>
      </div>
    </div>
  );
}

window.DeviceDiscoveryCard = DeviceDiscoveryCard;
