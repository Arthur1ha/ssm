/* DevicesScreen — 主屏设备分组列表 */
function DevicesScreen({ agents, unitData }) {
  const { useState: useStateD } = React;
  const [sensorsOpen, setSensorsOpen] = useStateD(true);

  const agentDevices = agents.filter(a => a.agent_type === 'robot' || a.agent_type === 'actuator');
  const sensors      = agents.filter(a => a.agent_type !== 'robot' && a.agent_type !== 'actuator');

  const sectionLabel = (text, count, collapsible, open, onToggle) => (
    <div onClick={collapsible ? onToggle : undefined}
      style={{ padding: '16px 6px 8px', display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        cursor: collapsible ? 'pointer' : 'default', userSelect: 'none' }}>
      <span style={{ fontSize: 13, color: 'rgba(255,255,255,0.45)', fontWeight: 500 }}>{text}</span>
      {collapsible && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          {!open && count > 0 && (
            <span style={{ fontSize: 10, color: 'rgba(255,255,255,0.2)', fontFamily: 'monospace' }}>
              {count} 个
            </span>
          )}
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
            stroke="rgba(255,255,255,0.3)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
            style={{ transition: 'transform 0.2s', transform: open ? 'rotate(0deg)' : 'rotate(-90deg)' }}>
            <path d="M6 9l6 6 6-6"/>
          </svg>
        </div>
      )}
    </div>
  );

  if (agents.length === 0) {
    return (
      <div style={{ padding: '40px 20px', textAlign: 'center',
        color: 'rgba(255,255,255,0.25)', fontSize: 13 }}>
        等待设备上线…
      </div>
    );
  }

  return (
    <div style={{ padding: '0 14px' }}>
      {agentDevices.length > 0 && (
        <>
          {sectionLabel('智能体')}
          {agentDevices.map(a => {
            if (a.agent_type === 'robot') {
              const meta = getAgentMeta(a);
              const slug = a.slug || a.unit_id || a.agent_id;
              return (
                <div key={slug} onClick={() => navigate('#/devices/' + slug)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 12,
                    padding: '12px 14px', marginBottom: 8, borderRadius: 18, cursor: 'pointer',
                    background: `linear-gradient(135deg, ${meta.color}15, rgba(255,255,255,0.03))`,
                    border: `1px solid ${meta.color}40`,
                  }}>
                  <div style={{ width: 42, height: 42, borderRadius: 13, flexShrink: 0,
                    background: meta.color, color: '#0B0B0E',
                    display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                    <Icon name={meta.icon} size={19}/>
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 14, fontWeight: 500 }}>{a.name || slug}</div>
                    <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.28)', marginTop: 2 }}>
                      {meta.label} · 点击控制
                    </div>
                  </div>
                  <Icon name="arrow" size={16} color="rgba(255,255,255,0.3)"/>
                </div>
              );
            }
            return <ActuatorCard key={a.unit_id || a.agent_id} agent={a} unitData={unitData}/>;
          })}
        </>
      )}
      {sensors.length > 0 && (
        <>
          {sectionLabel('传感器', sensors.length, true, sensorsOpen, () => setSensorsOpen(v => !v))}
          {sensorsOpen && sensors.map(a => (
            <SensorCard key={a.unit_id || a.agent_id} agent={a} unitData={unitData}/>
          ))}
        </>
      )}
    </div>
  );
}

window.DevicesScreen = DevicesScreen;
