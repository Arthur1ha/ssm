const { useState: useStateR, useEffect: useEffectR } = React;

function RulesScreen({ embedded = false }) {
  const [rules, setRules] = useStateR([]);

  const load = async () => {
    try {
      const r = await fetch('/api/rules');
      setRules(await r.json());
    } catch {}
  };

  useEffectR(() => { load(); }, []);

  const handleDelete = async (rule_id) => {
    await fetch(`/api/rules/${rule_id}`, { method: 'DELETE' });
    load();
  };

  const handleToggle = async (rule_id, enabled) => {
    await fetch(`/api/rules/${rule_id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !enabled }),
    });
    load();
  };

  return (
    <div style={{ position: embedded ? 'relative' : 'absolute', inset: embedded ? 'unset' : 0, display: 'flex', flexDirection: 'column' }}>
      <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none',
        background: 'radial-gradient(50% 30% at 50% 5%, rgba(200,255,62,0.1), transparent 70%)' }}/>
      <div style={{ padding: '14px 20px 10px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexShrink: 0, position: 'relative' }}>
        <span style={{ fontSize: 22, fontWeight: 300, letterSpacing: '-0.01em' }}>规则</span>
        <span style={{ fontSize: 11, color: 'rgba(255,255,255,0.35)' }}>
          {rules.filter(r => r.enabled).length} 启用 · {rules.length} 总计
        </span>
      </div>
      <div style={{ flex: 1, overflowY: 'auto', padding: '0 14px',
        paddingBottom: embedded ? '16px' : 'calc(158px + env(safe-area-inset-bottom, 0px))', position: 'relative' }}>
        {rules.length === 0 ? (
          <div style={{ padding: '60px 20px', textAlign: 'center', color: 'rgba(255,255,255,0.25)', fontSize: 13, lineHeight: 2 }}>
            还没有规则<br/>
            <span style={{ fontSize: 12 }}>在对话框里说"检测到人就开灯"来创建</span>
          </div>
        ) : rules.map(rule => (
          <div key={rule.rule_id} style={{
            display: 'flex', alignItems: 'center', gap: 12,
            padding: '13px 14px', marginBottom: 8, borderRadius: 18,
            background: rule.enabled ? 'rgba(200,255,62,0.05)' : 'rgba(255,255,255,0.03)',
            border: `1px solid ${rule.enabled ? 'rgba(200,255,62,0.16)' : 'rgba(255,255,255,0.06)'}`,
          }}>
            <div onClick={() => handleToggle(rule.rule_id, rule.enabled)}
              style={{ width: 38, height: 22, borderRadius: 11, flexShrink: 0, cursor: 'pointer',
                background: rule.enabled ? LIME : 'rgba(255,255,255,0.12)', position: 'relative',
                transition: 'background 0.2s' }}>
              <div style={{ position: 'absolute', top: 3, left: rule.enabled ? 18 : 3,
                width: 16, height: 16, borderRadius: '50%',
                background: '#0B0B0E', transition: 'left 0.2s' }}/>
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 14, fontWeight: 500,
                color: rule.enabled ? '#fff' : 'rgba(255,255,255,0.4)',
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {rule.name}
              </div>
              <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.28)', fontFamily: 'monospace', marginTop: 2 }}>
                {rule.trigger?.agent_tag}.{rule.trigger?.event} → {rule.action?.resource_tag}
              </div>
            </div>
            <button onClick={() => handleDelete(rule.rule_id)}
              style={{ width: 28, height: 28, borderRadius: '50%', flexShrink: 0, padding: 0,
                background: 'rgba(255,82,82,0.1)', border: '1px solid rgba(255,82,82,0.2)',
                color: 'rgba(255,82,82,0.7)', cursor: 'pointer',
                display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <Icon name="x" size={12}/>
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

window.RulesScreen = RulesScreen;
