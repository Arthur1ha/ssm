/* AppV2 — V2 根：主屏完整复用 V1 体验，设备详情页换成状态机驱动的 FsmDevicePage */
const GO2_STATIC_V2 = {
  unit_id: 'go2', name: 'Go2 Air', agent_type: 'robot', _online: false,
  capabilities: ['MOVE', 'STAND_UP', 'SIT_DOWN', 'HELLO', 'STRETCH', 'DANCE'],
};
const SUGGESTIONS_V2 = ['我要工作了', '帮我营造睡眠氛围', '有人来了', '我要离开了'];

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
  const { useState } = React;
  const { connected, agents, unitData } = useSsmCore();
  const { thinking, thinkingText, send } = useSendIntent();
  const currentHash = useHashLocal();

  const [activityLog, setActivityLog] = useState([]);
  const [rulesOpen, setRulesOpen]     = useState(false);
  const [pendingRule, setPendingRule] = useState(null);
  const [savingRule, setSavingRule]   = useState(false);

  const appendActivity = entry => setActivityLog(prev => [...prev, entry]);

  const handleSend = text => {
    const t = text.trim();
    if (!t) return;
    appendActivity({ type: 'user', text: t });
    send(t, {
      onMessage:     msg  => appendActivity({ type: 'ai', agent: 'orchestrator', text: msg }),
      onPendingRule: rule => setPendingRule(rule),
    });
  };

  const handleConfirmRule = () => {
    if (!pendingRule) return;
    setSavingRule(true);
    fetch('/api/rules', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(pendingRule),
    }).then(() => { setPendingRule(null); setSavingRule(false); })
      .catch(() => setSavingRule(false));
  };
  const handleCancelRule = () => setPendingRule(null);

  /* ── 设备路由：统一进 FsmDevicePage ── */
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

      {/* 头部 */}
      <div style={{ padding: '12px 16px', display: 'flex',
        alignItems: 'center', justifyContent: 'space-between', flexShrink: 0,
        borderBottom: '1px solid var(--color-border)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 17, fontWeight: 700, letterSpacing: '0.08em' }}>SSM</span>
          <span style={{ fontSize: 9, color: 'var(--color-accent)', letterSpacing: '0.2em' }}>V2</span>
          <div style={{ width: 7, height: 7, borderRadius: '50%',
            background: connected ? 'var(--color-accent)' : 'var(--color-danger)',
            boxShadow: connected ? '0 0 8px var(--color-online-glow)' : 'none' }}/>
        </div>
        <button onClick={() => setRulesOpen(true)} className="btn">
          <Icon name="list" size={13}/> 规则
        </button>
      </div>

      {/* 可滚动主体 */}
      <div style={{ flex: 1, overflowY: 'auto' }}>
        <DevicesScreen agents={agents} unitData={unitData}/>

        {activityLog.length > 0 && (
          <div style={{ padding: '16px 20px 8px', marginTop: 4 }}>
            <span style={{ fontSize: 13, color: 'var(--color-text-dim)', fontWeight: 500 }}>活动</span>
          </div>
        )}

        <ActivityFeed entries={activityLog} thinking={thinking} thinkingText={thinkingText}/>

        {pendingRule && (
          <div style={{ padding: '0 16px 10px' }}>
            <div style={{ background: 'var(--color-accent-dim)',
              border: '1px solid rgba(200,255,62,0.22)', borderRadius: 18, padding: '14px 16px' }}>
              <div style={{ fontSize: 12, color: '#C8FF3E', fontWeight: 600, marginBottom: 8 }}>
                规则预览 · 确认保存？</div>
              <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 4 }}>{pendingRule.name}</div>
              <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.4)', fontFamily: 'monospace', marginBottom: 12 }}>
                当 {pendingRule.trigger?.tag}.{pendingRule.trigger?.event}
                {' → '}{pendingRule.action?.tag} / {pendingRule.action?.cmd}
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <button onClick={handleCancelRule} style={{
                  flex: 1, padding: '9px 0', borderRadius: 999, fontSize: 13,
                  background: 'var(--color-surface-2)', border: '1px solid var(--color-border)',
                  color: 'var(--color-text-muted)', cursor: 'pointer', fontFamily: 'inherit' }}>取消</button>
                <button onClick={handleConfirmRule} disabled={savingRule} style={{
                  flex: 2, padding: '9px 0', borderRadius: 999, fontSize: 13, fontWeight: 600,
                  background: 'var(--color-accent)', border: 'none', color: 'var(--color-bg)',
                  cursor: 'pointer', fontFamily: 'inherit' }}>
                  {savingRule ? '保存中...' : '确认保存'}</button>
              </div>
            </div>
          </div>
        )}
        <div style={{ height: 16 }}/>
      </div>

      {/* 输入栏 */}
      <div style={{ flexShrink: 0, background: 'var(--color-bar)',
        backdropFilter: 'var(--glass-blur)', WebkitBackdropFilter: 'var(--glass-blur)',
        borderTop: '1px solid var(--color-border)' }}>
        {activityLog.every(e => e.type !== 'user') && !thinking && (
          <div style={{ padding: '8px 12px 0', display: 'flex', gap: 8,
            overflowX: 'auto', scrollbarWidth: 'none' }}>
            {SUGGESTIONS_V2.map(s => (
              <button key={s} onClick={() => handleSend(s)} style={{
                flexShrink: 0, padding: '8px 16px', borderRadius: 'var(--radius-card)',
                background: 'var(--color-surface-2)', border: '1px solid var(--color-border-strong)',
                color: 'var(--color-text-muted)', fontSize: 13, cursor: 'pointer',
                fontFamily: 'inherit', whiteSpace: 'nowrap',
                WebkitTapHighlightColor: 'transparent' }}>{s}</button>
            ))}
          </div>
        )}
        <MainInputBar onSend={handleSend} thinking={thinking}/>
      </div>

      <RulesDrawer open={rulesOpen} onClose={() => setRulesOpen(false)}/>
    </div>
  );
}
window.AppV2 = AppV2;
