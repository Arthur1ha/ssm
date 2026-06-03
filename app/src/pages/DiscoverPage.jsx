function DiscoverScreen({ agents, connected, phoneLoc, locError }) {
  const sensorCnt   = agents.filter(a => a.agent_type !== 'actuator').length;
  const actuatorCnt = agents.filter(a => a.agent_type === 'actuator').length;

  return (
    <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column' }}>
      <div style={{ position: 'absolute', inset: 0, background: 'radial-gradient(60% 45% at 50% 40%, rgba(200,255,62,0.06), transparent 70%)', pointerEvents: 'none' }}/>
      <div style={{ padding: '14px 20px 0', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexShrink: 0, position: 'relative' }}>
        <div>
          <span style={{ fontSize: 22, fontWeight: 300, letterSpacing: '-0.01em' }}>附近</span>
          <span style={{ fontSize: 11, color: 'rgba(255,255,255,0.35)', marginLeft: 10 }}>
            {agents.length === 0 ? '扫描中…' : `${agents.length} 个在线`}
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 10, color: phoneLoc ? 'rgba(200,255,62,0.6)' : 'rgba(255,255,255,0.3)', fontFamily: 'monospace' }}>
            {phoneLoc ? `${phoneLoc.lat.toFixed(3)}, ${phoneLoc.lng.toFixed(3)}` : locError || '定位中…'}
          </span>
          <div style={{ width: 6, height: 6, borderRadius: '50%',
            background: connected ? LIME : '#FF5252',
            boxShadow: connected ? `0 0 6px ${LIME}` : 'none' }}/>
        </div>
      </div>
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
        paddingBottom: 'calc(158px + env(safe-area-inset-bottom, 0px))', position: 'relative' }}>
        <RadarScan agents={agents} phoneLoc={phoneLoc}/>
        <div style={{ marginTop: 24, display: 'flex', alignItems: 'center', gap: 28 }}>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 20, fontWeight: 300, color: LIME }}>{sensorCnt}</div>
            <div style={{ fontSize: 10, color: 'rgba(255,255,255,0.3)', fontFamily: 'monospace', marginTop: 3 }}>传感器</div>
          </div>
          <div style={{ width: 1, height: 28, background: 'rgba(255,255,255,0.08)' }}/>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 20, fontWeight: 300, color: '#FF9A5A' }}>{actuatorCnt}</div>
            <div style={{ fontSize: 10, color: 'rgba(255,255,255,0.3)', fontFamily: 'monospace', marginTop: 3 }}>执行器</div>
          </div>
        </div>
        {agents.length === 0 && (
          <div style={{ marginTop: 18, fontSize: 12, color: 'rgba(255,255,255,0.25)' }}>等待 ESP32 上线...</div>
        )}
      </div>
    </div>
  );
}

window.DiscoverScreen = DiscoverScreen;
