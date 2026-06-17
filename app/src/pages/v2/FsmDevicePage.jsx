/* FsmDevicePage — 通用状态机设备页（V2）。读 card.state_machine + 实时当前态。 */
function FsmDevicePage({ unitId, device, liveState, onBack }) {
  const { useState, useEffect, useRef } = React;
  const [sm, setSm]       = useState(null);       // card.state_machine
  const [transport, setTransport] = useState(null);
  const [sseState, setSseState]   = useState(null);
  const [open, setOpen]   = useState(false);
  const esRef = useRef(null);

  const isGo2 = unitId === 'go2';

  /* 拉取 Agent Card 拓扑 */
  useEffect(() => {
    fetch('/api/devices/' + unitId + '/agent')
      .then(r => r.json())
      .then(c => { setSm(c.state_machine || null); setTransport(c.transport || null); })
      .catch(() => {});
  }, [unitId]);

  /* Go2 当前态走 SSE；ESP32 当前态来自 liveState（ISM state.ism） */
  useEffect(() => {
    if (!isGo2) return;
    const es = new EventSource('/api/go2/connection/stream');
    esRef.current = es;
    es.onmessage = e => {
      try { const d = JSON.parse(e.data); if (d.fsm_state) setSseState(d.fsm_state); } catch (_) {}
    };
    return () => es.close();
  }, [isGo2]);

  const current = isGo2 ? (sseState || 'offline')
                        : (liveState?.state?.ism || sm?.initial || '');

  /* 派发一条转移 */
  const fire = (trigger) => {
    if (isGo2 || transport?.kind === 'http') {
      fetch('/api/go2/commands', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cmd: trigger }),
      }).catch(() => {});
    } else {
      console.warn('[V2] ssm-fire dispatched (no MQTT handler yet):', { unitId, trigger });
      window.dispatchEvent(new CustomEvent('ssm-fire', { detail: { unitId, trigger } }));
    }
  };

  const ACCENT = 'var(--color-accent)';
  const outgoing = (sm?.transitions || []).filter(t => t.src === current);

  return (
    <div style={{ position: 'fixed', inset: 0, background: 'var(--color-bg)', color: '#ccc',
      fontFamily: 'var(--font-sans)', paddingTop: 'env(safe-area-inset-top,0px)',
      display: 'flex', flexDirection: 'column' }}>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '11px 16px',
        borderBottom: '1px solid var(--color-border)', background: 'var(--color-surface-1)' }}>
        <button onClick={onBack} style={{ background: 'none', border: 'none',
          color: 'var(--color-text-dim)', cursor: 'pointer', fontSize: 18 }}>←</button>
        <div style={{ fontSize: 13, fontWeight: 700, letterSpacing: '0.15em', color: ACCENT,
          textShadow: '0 0 14px var(--color-accent)' }}>{(device?.name || unitId).toUpperCase()}</div>
        <span style={{ marginLeft: 'auto', fontSize: 10, color: ACCENT, letterSpacing: '0.1em',
          fontFamily: 'var(--font-mono)' }}>{(current || '—').toUpperCase()}</span>
      </div>

      <div style={{ flex: 1, overflowY: 'auto', padding: '16px' }}>

        {/* 当前态大卡 */}
        <div style={{ textAlign: 'center', padding: '22px 0', border: '1px solid var(--color-accent-border)',
          borderRadius: 'var(--radius-sm)', background: 'var(--color-accent-dim)' }}>
          <div style={{ fontSize: 9, color: 'var(--color-text-dim)', letterSpacing: '0.25em' }}>CURRENT STATE</div>
          <div style={{ fontSize: 28, fontWeight: 700, color: ACCENT, marginTop: 6,
            textShadow: '0 0 18px var(--color-accent)', fontFamily: 'var(--font-mono)' }}>
            {(current || '—').toUpperCase()}</div>
        </div>

        {/* 态内富控件插槽 */}
        {window.fsmWidget && window.fsmWidget(unitId, current)}

        {/* 可执行转移按钮 */}
        <div style={{ marginTop: 14, display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 8 }}>
          {outgoing.length === 0 && (
            <div style={{ gridColumn: '1/4', textAlign: 'center', fontSize: 11,
              color: 'var(--color-text-dim)', padding: '10px 0', letterSpacing: '0.15em' }}>
              — 当前态无可执行转移 —</div>
          )}
          {outgoing.map(t => (
            <button key={t.trigger + t.dst} onClick={() => fire(t.trigger)} style={{
              padding: '12px 0', background: 'var(--color-accent-dim)', color: ACCENT,
              border: '1px solid rgba(200,255,62,0.28)', borderRadius: 'var(--radius-sm)',
              fontSize: 12, fontWeight: 700, cursor: 'pointer', fontFamily: 'var(--font-mono)',
              letterSpacing: '0.05em', WebkitTapHighlightColor: 'transparent' }}>
              {t.label}<div style={{ fontSize: 8, opacity: 0.5, marginTop: 2 }}>→ {t.dst}</div>
            </button>
          ))}
        </div>

        {/* 完整状态图（可折叠） */}
        <button onClick={() => setOpen(o => !o)} style={{ marginTop: 16, width: '100%',
          background: 'none', border: 'none', color: 'var(--color-text-dim)', fontSize: 9,
          letterSpacing: '0.2em', textAlign: 'left', cursor: 'pointer' }}>
          STATE GRAPH {open ? '▴' : '▾'}</button>
        {open && sm && (
          <div style={{ marginTop: 8, fontFamily: 'var(--font-mono)', fontSize: 10 }}>
            {sm.transitions.map(t => (
              <div key={t.src + t.trigger + t.dst} style={{ padding: '3px 0',
                color: t.src === current ? ACCENT : 'var(--color-text-dim)' }}>
                {t.src} ──{t.label}──▸ {t.dst}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
window.FsmDevicePage = FsmDevicePage;
