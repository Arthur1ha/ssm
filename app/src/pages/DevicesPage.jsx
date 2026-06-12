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
      <span style={{ fontSize: 13, color: 'var(--color-text-dim)', fontWeight: 500 }}>{text}</span>
      {collapsible && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          {!open && count > 0 && (
            <span style={{ fontSize: 10, color: 'var(--color-text-dim)', fontFamily: 'monospace' }}>
              {count} 个
            </span>
          )}
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
            stroke="var(--color-text-dim)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
            style={{ transition: 'transform 0.2s', transform: open ? 'rotate(0deg)' : 'rotate(-90deg)' }}>
            <path d="M6 9l6 6 6-6"/>
          </svg>
        </div>
      )}
    </div>
  );

  if (agents.length === 0) {
    return (
      <div style={{ padding: '40px 20px', textAlign: 'center' }}>
        <div style={{ fontSize: 13, color: 'var(--color-text-dim)', marginBottom: 8 }}>
          等待设备上线…
        </div>
        <div style={{ fontSize: 11, color: 'var(--color-text-dim)', opacity: 0.6, lineHeight: 1.6 }}>
          请确认 ESP32 已开机并连接到同一网络
        </div>
      </div>
    );
  }

  return (
    <div style={{ padding: '0 14px' }}>
      {agentDevices.length > 0 && (
        <>
          {sectionLabel('智能体')}
          {agentDevices.map(a => (
            <ActuatorCard key={a.unit_id} agent={a} unitData={unitData}/>
          ))}
        </>
      )}
      {sensors.length > 0 && (
        <>
          {sectionLabel('传感器', sensors.length, true, sensorsOpen, () => setSensorsOpen(v => !v))}
          {sensorsOpen && sensors.map(a => (
            <SensorCard key={a.unit_id} agent={a} unitData={unitData}/>
          ))}
        </>
      )}
    </div>
  );
}

window.DevicesScreen = DevicesScreen;
