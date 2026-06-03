function DevicesScreen({ agents, unitData }) {
  const robots      = agents.filter(a => a.agent_type === 'robot');
  const actuators   = agents.filter(a => a.agent_type === 'actuator');
  const sensors     = agents.filter(a => a.agent_type !== 'actuator' && a.agent_type !== 'robot');
  const activeCount = actuators.filter(a => isAgentActive(a, unitData)).length;

  const sectionLabel = (text) => (
    <div style={{ padding: '4px 2px 8px', marginTop: 6 }}>
      <span style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
        color: 'rgba(255,255,255,0.28)', fontWeight: 600 }}>{text}</span>
    </div>
  );

  return (
    <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column' }}>
      <div style={{ position: 'absolute', inset: 0, background: 'radial-gradient(50% 30% at 80% 5%, rgba(226,107,255,0.18), transparent 70%)', pointerEvents: 'none' }}/>
      <div style={{ padding: '14px 20px 10px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexShrink: 0, position: 'relative' }}>
        <span style={{ fontSize: 22, fontWeight: 300, letterSpacing: '-0.01em' }}>设备</span>
        <span style={{ fontSize: 11, color: 'rgba(255,255,255,0.35)' }}>
          {agents.length} 个 · {activeCount} 活跃
        </span>
      </div>
      <div style={{ flex: 1, overflowY: 'auto', padding: '0 14px',
        paddingBottom: 'calc(158px + env(safe-area-inset-bottom, 0px))', position: 'relative' }}>
        {agents.length === 0 ? (
          <div style={{ padding: '60px 20px', textAlign: 'center' }}>
            <div style={{ width: 60, height: 60, borderRadius: '50%', background: 'rgba(255,255,255,0.04)', margin: '0 auto 16px', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <Icon name="wifi" size={24} color="rgba(255,255,255,0.25)"/>
            </div>
            <div style={{ fontSize: 20, fontWeight: 300, marginBottom: 8 }}>等待设备上线</div>
            <div style={{ fontSize: 13, color: 'rgba(255,255,255,0.4)', lineHeight: 1.6, maxWidth: 220, margin: '0 auto' }}>
              确认 ESP32 已连接到 MQTT Broker
            </div>
          </div>
        ) : (
          <>
            {robots.length > 0 && (
              <>
                {sectionLabel('机器人')}
                {robots.map(a => {
                  const meta = getAgentMeta(a);
                  const slug = a.slug || a.unit_id || a.agent_id;
                  return (
                    <div key={a.unit_id || a.agent_id}
                      onClick={() => navigate('#/devices/' + slug)}
                      style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '12px 14px',
                        marginBottom: 8, borderRadius: 18, cursor: 'pointer',
                        background: `linear-gradient(135deg, ${meta.color}15, rgba(255,255,255,0.03))`,
                        border: `1px solid ${meta.color}40` }}>
                      <div style={{ width: 42, height: 42, borderRadius: 13, flexShrink: 0,
                        background: meta.color, color: '#0B0B0E',
                        display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                        <Icon name={meta.icon} size={19}/>
                      </div>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: 14, fontWeight: 500 }}>{a.name || slug}</div>
                        <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.28)', marginTop: 2 }}>{meta.label} · 点击控制</div>
                      </div>
                      <Icon name="arrow" size={16} color="rgba(255,255,255,0.3)"/>
                    </div>
                  );
                })}
              </>
            )}
            {actuators.length > 0 && (
              <>
                {sectionLabel('执行器')}
                {actuators.map(a => <ActuatorCard key={a.unit_id || a.agent_id} agent={a} unitData={unitData}/>)}
              </>
            )}
            {sensors.length > 0 && (
              <>
                {sectionLabel('传感器')}
                {sensors.map(a => <SensorCard key={a.unit_id || a.agent_id} agent={a} unitData={unitData}/>)}
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}

window.DevicesScreen = DevicesScreen;
