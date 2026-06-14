const { useState: useStateR, useEffect: useEffectR } = React;

function RulesScreen({ embedded = false, onRulesChange, open }) {
  const [rules, setRules] = useStateR([]);
  const [pendingDelete, setPendingDelete] = useStateR(null); // rule_id | null

  // 自动取消确认（1.5 秒超时）
  const deleteTimerRef = React.useRef(null);
  const requestDelete = (rule_id) => {
    if (pendingDelete === rule_id) {
      // 二次点击：执行删除
      clearTimeout(deleteTimerRef.current);
      setPendingDelete(null);
      handleDelete(rule_id);
    } else {
      // 第一次点击：进入确认状态
      clearTimeout(deleteTimerRef.current);
      setPendingDelete(rule_id);
      deleteTimerRef.current = setTimeout(() => setPendingDelete(null), 1500);
    }
  };

  const load = async () => {
    try {
      const r = await fetch('/api/rules');
      const data = await r.json();
      setRules(data);
      onRulesChange?.(data);
    } catch {}
  };

  useEffectR(() => { load(); }, []);
  useEffectR(() => { if (open) load(); }, [open]);

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
    <div style={{ position: embedded ? 'relative' : 'absolute', inset: embedded ? 'unset' : 0,
      display: 'flex', flexDirection: 'column' }}>
      <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none',
        background: 'radial-gradient(50% 30% at 50% 5%, var(--color-accent-dim), transparent 70%)' }}/>
      {!embedded && (
        <div style={{ padding: '14px 20px 10px', display: 'flex', alignItems: 'center',
          justifyContent: 'space-between', flexShrink: 0, position: 'relative' }}>
          <span style={{ fontSize: 22, fontWeight: 300, letterSpacing: '-0.01em',
            color: 'var(--color-text)' }}>规则</span>
          <span style={{ fontSize: 11, color: 'var(--color-text-dim)' }}>
            {rules.filter(r => r.enabled).length} 启用 · {rules.length} 总计
          </span>
        </div>
      )}
      <div style={{ flex: 1, overflowY: 'auto', padding: '0 14px',
        paddingBottom: embedded ? '16px' : 'calc(158px + env(safe-area-inset-bottom, 0px))',
        position: 'relative' }}>
        {rules.length === 0 ? (
          <div style={{ padding: '60px 20px', textAlign: 'center',
            color: 'var(--color-text-dim)', fontSize: 13, lineHeight: 2 }}>
            还没有规则<br/>
            <span style={{ fontSize: 12 }}>在对话框里说"检测到人就开灯"来创建</span><br/>
            <span style={{ fontSize: 11, color: 'var(--color-text-dim)',
              opacity: 0.6 }}>规则创建后会在这里显示，可随时启用或关闭</span>
          </div>
        ) : rules.map(rule => (
          <div key={rule.rule_id} style={{
            display: 'flex', alignItems: 'center', gap: 12,
            padding: '13px 14px', marginBottom: 8,
            borderRadius: 'var(--radius-card)',
            background: rule.enabled ? 'var(--color-accent-dim)' : 'var(--color-surface-1)',
            border: `1px solid ${rule.enabled ? 'rgba(200,255,62,0.16)' : 'var(--color-border)'}`,
          }}>
            {/* Toggle */}
            <div onClick={() => handleToggle(rule.rule_id, rule.enabled)}
              className="interactive"
              style={{ width: 38, height: 22, borderRadius: 11, flexShrink: 0,
                background: rule.enabled ? 'var(--color-accent)' : 'var(--color-surface-3)',
                position: 'relative', transition: 'background 0.2s' }}>
              <div style={{ position: 'absolute', top: 3,
                left: rule.enabled ? 18 : 3,
                width: 16, height: 16, borderRadius: '50%',
                background: 'var(--color-bg)', transition: 'left 0.2s' }}/>
            </div>
            {/* 规则信息 */}
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 14, fontWeight: 500,
                color: rule.enabled ? 'var(--color-text)' : 'var(--color-text-muted)',
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {rule.name}
              </div>
              <div style={{ fontSize: 11, color: 'var(--color-text-dim)',
                fontFamily: 'var(--font-mono)', marginTop: 2 }}>
                {rule.trigger?.tag}.{rule.trigger?.event} → {rule.action?.tag}
              </div>
            </div>
            {/* 删除按钮（扩大触摸目标 B6 + 二次确认 B3） */}
            <button
              onClick={() => requestDelete(rule.rule_id)}
              style={{
                flexShrink: 0, padding: 8, margin: -8,  /* 视觉28px，点击44px */
                borderRadius: '50%',
                background: pendingDelete === rule.rule_id
                  ? 'var(--color-danger-dim)' : 'transparent',
                border: pendingDelete === rule.rule_id
                  ? '1px solid var(--color-danger-border)' : '1px solid transparent',
                color: pendingDelete === rule.rule_id
                  ? 'var(--color-danger)' : 'var(--color-text-dim)',
                cursor: 'pointer', transition: 'all 0.15s',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                fontSize: pendingDelete === rule.rule_id ? 10 : 12,
                fontFamily: 'var(--font-sans)',
                WebkitTapHighlightColor: 'transparent',
              }}>
              {pendingDelete === rule.rule_id ? '确认?' : <Icon name="x" size={12}/>}
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

window.RulesScreen = RulesScreen;
