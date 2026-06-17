/* AppV2 — V2 根：主屏复用 V1 体验，设备详情页换成状态机驱动的 FsmDevicePage */
function useHashLocal() {
  const { useState, useEffect } = React;
  const [h, setH] = useState(window.location.hash);
  useEffect(() => {
    const f = () => setH(window.location.hash);
    window.addEventListener('hashchange', f);
    return () => window.removeEventListener('hashchange', f);
  }, []);
  return h;
}

function AppV2() {
  const { connected, agents, unitData } = useSsmCore();
  const currentHash = useHashLocal();

  const m = currentHash.match(/^#\/devices\/([^/]+)$/);
  if (m) {
    const uid = m[1];
    const device = agents.find(a => a.unit_id === uid) || { unit_id: uid, name: uid };
    return <FsmDevicePage unitId={uid} device={device}
      liveState={unitData[uid]} onBack={() => navigate('#')}/>;
  }

  return (
    <div style={{ position: 'fixed', inset: 0, background: 'var(--bg-gradient)', color: '#fff',
      fontFamily: 'var(--font-sans)', paddingTop: 'env(safe-area-inset-top,0px)',
      display: 'flex', flexDirection: 'column' }}>
      <div style={{ padding: '12px 16px', borderBottom: '1px solid var(--color-border)',
        display: 'flex', alignItems: 'center', gap: 10 }}>
        <span style={{ fontSize: 17, fontWeight: 700, letterSpacing: '0.08em' }}>SSM</span>
        <span style={{ fontSize: 9, color: 'var(--color-accent)', letterSpacing: '0.2em' }}>V2 · FSM</span>
        <div style={{ marginLeft: 'auto', width: 7, height: 7, borderRadius: '50%',
          background: connected ? 'var(--color-accent)' : 'var(--color-danger)' }}/>
      </div>
      <div style={{ flex: 1, overflowY: 'auto' }}>
        <DevicesScreen agents={agents} unitData={unitData}/>
      </div>
    </div>
  );
}
window.AppV2 = AppV2;
