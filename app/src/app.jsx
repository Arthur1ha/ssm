/* SSM PWA — App 根组件：单主屏 + Hash 路由全屏设备页 */
const { useState, useEffect, useRef } = React;

// LIME 来自 config.js 全局，此处不重复声明
const EXCL_TYPES = new Set(['decision', 'supervisor']);
const EXCL_PLAT  = new Set(['pc', 'pwa']);
const SUGGESTIONS = ['我要工作了', '帮我营造睡眠氛围', '有人来了', '我要离开了'];

const GO2_STATIC_DEVICE = {
  unit_id: 'go2', agent_id: 'go2', slug: 'go2',
  name: 'Go2 Air', agent_type: 'robot',
  capabilities: ['MOVE', 'STAND_UP', 'SIT_DOWN', 'HELLO', 'STRETCH', 'DANCE'],
};

/* ── Hash 路由 ── */
function useHash() {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const handler = () => setHash(window.location.hash);
    window.addEventListener('hashchange', handler);
    return () => window.removeEventListener('hashchange', handler);
  }, []);
  return hash;
}

/* ── App ── */
function App() {
  const [connected, setConnected]   = useState(false);
  const [agents, setAgents]         = useState([GO2_STATIC_DEVICE]);
  const [unitData, setUnitData]     = useState({});
  const [activityLog, setActivityLog] = useState([]);
  const [rulesOpen, setRulesOpen]   = useState(false);
  const [pendingRule, setPendingRule] = useState(null);
  const [savingRule, setSavingRule]  = useState(false);

  const { thinking, thinkingText, send } = useSendIntent();
  const currentHash = useHash();
  const agentsRef   = useRef([GO2_STATIC_DEVICE]);
  const prevStatesRef = useRef({});

  useEffect(() => { agentsRef.current = agents; }, [agents]);

  const appendActivity = (entry) =>
    setActivityLog(prev => [...prev.slice(-19), { ...entry, ts: Date.now() }]);

  /* ── MQTT 初期化 ── */
  useEffect(() => {
    const registry   = new AgentRegistry(mqttBus);
    const ismTracker = new ISMTracker(mqttBus);

    const handleRegistryChange = () => {
      const mqttAgents = registry.getAll().filter(a =>
        a.agent_type && !EXCL_TYPES.has(a.agent_type) && !EXCL_PLAT.has(a.hw_platform) && a._online === true
      );
      setAgents([GO2_STATIC_DEVICE, ...mqttAgents]);
    };
    registry.addEventListener('change', handleRegistryChange);

    let pendingSnap = false;
    const handleIsmUpdate = () => {
      if (pendingSnap) return;
      pendingSnap = true;
      requestAnimationFrame(() => {
        pendingSnap = false;
        const snap = {};
        ismTracker.unitIds().forEach(uid => {
          snap[uid] = {
            state:  ismTracker.get(uid, 'state'),
            event:  ismTracker.get(uid, 'event'),
            report: ismTracker.get(uid, 'report'),
          };
          /* ISM 状态变化 → 追加活动事件 */
          const ism = snap[uid].state?.ism;
          if (ism && ism !== prevStatesRef.current[uid]) {
            if (prevStatesRef.current[uid] !== undefined) {
              const agent = agentsRef.current.find(a => (a.unit_id || a.agent_id) === uid);
              const name  = agent?.name || uid;
              appendActivity({ type: 'event', text: `${name} → ${ism}` });
            }
            prevStatesRef.current[uid] = ism;
          }
        });
        setUnitData({ ...snap });
      });
    };
    ismTracker.addEventListener('update', handleIsmUpdate);

    const handleConnect = () => {
      setConnected(true);
      mqttBus.publish('ssm/agents/phone_ui/manifest', {
        unit_id: 'phone_ui', agent_type: 'supervisor',
        name: 'human_supervisor', hw_platform: 'pwa',
        ts: Math.floor(Date.now() / 1000),
      }, { retain: true });
      mqttBus.subscribe('ssm/agents/desk/speech');
    };
    const handleDisconnect = () => {
      setConnected(false);
      appendActivity({ type: 'system', text: 'MQTT 连接断开，正在重连…' });
    };
    const handleReconnect  = () => setConnected(false);
    mqttBus.addEventListener('connect',    handleConnect);
    mqttBus.addEventListener('disconnect', handleDisconnect);
    mqttBus.addEventListener('reconnect',  handleReconnect);
    mqttBus.connect(BROKER_URL, null, { username: BROKER_USER, password: BROKER_PASS });

    /* desk TTS */
    const handleSpeech = (e) => {
      const { audio } = e.detail || {};
      if (audio) playAudioB64(audio);
    };
    mqttBus.addEventListener('topic:ssm/agents/desk/speech', handleSpeech);

    return () => {
      registry.removeEventListener('change', handleRegistryChange);
      ismTracker.removeEventListener('update', handleIsmUpdate);
      mqttBus.removeEventListener('connect',    handleConnect);
      mqttBus.removeEventListener('disconnect', handleDisconnect);
      mqttBus.removeEventListener('reconnect',  handleReconnect);
      mqttBus.removeEventListener('topic:ssm/agents/desk/speech', handleSpeech);
    };
  }, []);

  /* ── 规则保存 ── */
  const handleConfirmRule = async () => {
    if (!pendingRule) return;
    setSavingRule(true);
    try {
      await fetch('/api/rules', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(pendingRule),
      });
      const saved = pendingRule;
      setPendingRule(null);
      appendActivity({ type: 'ai', text: `规则「${saved.name}」已保存，条件触发时自动执行。` });
    } catch {
      appendActivity({ type: 'ai', text: '规则保存失败，请重试。' });
    }
    setSavingRule(false);
  };

  const handleCancelRule = () => {
    setPendingRule(null);
    appendActivity({ type: 'ai', text: '已取消，规则未保存。' });
  };

  /* ── 主屏发送 ── */
  const handleSend = (text) => {
    const t = text.trim();
    if (!t) return;
    appendActivity({ type: 'user', text: t });

    send(t, {
      onMessage:     (msg)  => appendActivity({ type: 'ai', text: msg }),
      onPendingRule: (rule) => setPendingRule(rule),
    });
  };

  /* ── Hash 路由：全屏设备页 ── */
  const hashMatch = currentHash.match(/^#\/devices\/([^/]+)$/);
  if (hashMatch) {
    const slug = hashMatch[1];
    if (slug === 'go2') {
      return <Go2DevicePage onBack={() => navigate('#')}/>;
    }
    const device = agents.find(a => a.slug === slug || (a.unit_id || a.agent_id) === slug);
    if (!device) { navigate('#'); return null; }
    return (
      <DeviceDetailPage
        slug={slug}
        device={device}
        unitData={unitData}
        onBack={() => navigate('#')}
      />
    );
  }

  /* ── 主屏布局 ── */
  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'var(--color-bg)', color: '#fff',
      fontFamily: 'var(--font-sans)',
      paddingTop: 'env(safe-area-inset-top, 0px)',
      display: 'flex', flexDirection: 'column',
    }}>

      {/* 头部 */}
      <div style={{
        padding: '14px 20px 10px', display: 'flex',
        alignItems: 'center', justifyContent: 'space-between', flexShrink: 0,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 22, fontWeight: 300, letterSpacing: '-0.01em' }}>SSM</span>
          <div
            onClick={!connected ? () => mqttBus.connect(BROKER_URL, null, { username: BROKER_USER, password: BROKER_PASS }) : undefined}
            style={{
              width: 6, height: 6, borderRadius: '50%',
              background: connected ? 'var(--color-accent)' : 'var(--color-danger)',
              boxShadow: connected ? '0 0 6px var(--color-online-glow)' : 'none',
              cursor: connected ? 'default' : 'pointer',
            }}
          />
        </div>
        <button onClick={() => setRulesOpen(true)} style={{
          background: 'var(--color-surface-2)', border: '1px solid var(--color-border)',
          color: 'var(--color-text-muted)', borderRadius: 10, padding: '6px 10px',
          cursor: 'pointer', fontFamily: 'inherit', fontSize: 12,
          display: 'flex', alignItems: 'center', gap: 5,
        }}>
          <Icon name="zap" size={13}/>
          规则
        </button>
      </div>

      {/* 可滚动主体 */}
      <div style={{ flex: 1, overflowY: 'auto' }}>

        {/* 设备分组 */}
        <DevicesScreen agents={agents} unitData={unitData}/>

        {/* 活动分隔线 */}
        <div style={{ padding: '16px 20px 8px', marginTop: 4 }}>
          <span style={{ fontSize: 13, color: 'var(--color-text-dim)', fontWeight: 500 }}>活动</span>
        </div>

        {/* 活动流 */}
        <ActivityFeed
          entries={activityLog}
          thinking={thinking}
          thinkingText={thinkingText}
        />

        {/* 规则确认卡 */}
        {pendingRule && (
          <div style={{ padding: '0 16px 10px' }}>
            <div style={{
              background: 'var(--color-accent-dim)', border: '1px solid rgba(200,255,62,0.22)',
              borderRadius: 18, padding: '14px 16px',
            }}>
              <div style={{ fontSize: 12, color: LIME, fontWeight: 600, marginBottom: 8 }}>
                规则预览 · 确认保存？
              </div>
              <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 4 }}>{pendingRule.name}</div>
              <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.4)', fontFamily: 'monospace', marginBottom: 12 }}>
                当 {pendingRule.trigger?.agent_tag}.{pendingRule.trigger?.event}
                {' → '}
                {pendingRule.action?.resource_tag} / {pendingRule.action?.cmd}
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <button onClick={handleCancelRule} style={{
                  flex: 1, padding: '9px 0', borderRadius: 999, fontSize: 13,
                  background: 'var(--color-surface-2)', border: '1px solid var(--color-border)',
                  color: 'var(--color-text-muted)', cursor: 'pointer', fontFamily: 'inherit',
                }}>取消</button>
                <button onClick={handleConfirmRule} disabled={savingRule} style={{
                  flex: 2, padding: '9px 0', borderRadius: 999, fontSize: 13, fontWeight: 600,
                  background: 'var(--color-accent)', border: 'none', color: 'var(--color-bg)',
                  cursor: 'pointer', fontFamily: 'inherit',
                  boxShadow: '0 0 16px rgba(200,255,62,0.3)',
                }}>{savingRule ? '保存中...' : '确认保存'}</button>
              </div>
            </div>
          </div>
        )}

        {/* 底部留白（防止内容被输入栏遮住） */}
        <div style={{ height: 16 }}/>
      </div>

      {/* 快捷建议 + 输入栏 */}
      <div style={{ flexShrink: 0, background: 'var(--color-bg)' }}>
        {activityLog.length === 0 && !thinking && (
          <div style={{ padding: '8px 12px 0', display: 'flex', gap: 8,
            overflowX: 'auto', scrollbarWidth: 'none' }}>
            {SUGGESTIONS.map(s => (
              <button key={s} onClick={() => handleSend(s)} style={{
                flexShrink: 0, padding: '8px 16px', borderRadius: 'var(--radius-card)',
                background: 'var(--color-surface-2)', border: '1px solid var(--color-border-strong)',
                color: 'var(--color-text-muted)', fontSize: 13, cursor: 'pointer',
                fontFamily: 'inherit', whiteSpace: 'nowrap',
                WebkitTapHighlightColor: 'transparent',
              }}>{s}</button>
            ))}
          </div>
        )}
        <MainInputBar onSend={handleSend} thinking={thinking}/>
      </div>

      {/* 规则抽屉 */}
      <RulesDrawer open={rulesOpen} onClose={() => setRulesOpen(false)}/>
    </div>
  );
}

/* ── 底部输入栏 ── */
function MainInputBar({ onSend, thinking }) {
  const [input, setInput] = useState('');

  const handleSend = () => {
    const t = input.trim();
    if (!t || thinking) return;
    setInput('');
    onSend(t);
  };

  return (
    <div style={{
      flexShrink: 0, padding: '8px 12px',
      paddingBottom: 'calc(8px + env(safe-area-inset-bottom, 0px))',
      borderTop: '1px solid var(--color-border)',
      background: 'var(--color-bg)',
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '6px 6px 6px 16px',
        background: 'rgba(30,29,38,0.95)',
        border: '1px solid var(--color-border)',
        borderRadius: 999,
      }}>
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && !e.isComposing && handleSend()}
          placeholder={thinking ? '处理中…' : '告诉我想要什么…'}
          disabled={thinking}
          style={{
            flex: 1, background: 'transparent', border: 'none',
            color: thinking ? 'rgba(255,255,255,0.35)' : '#fff',
            fontSize: 14, fontFamily: 'inherit', outline: 'none',
            WebkitTapHighlightColor: 'transparent',
          }}
        />
        <button
          onClick={handleSend}
          disabled={!input.trim() || thinking}
          style={{
            width: 38, height: 38, borderRadius: 999, flexShrink: 0,
            background: input.trim() && !thinking ? 'var(--color-accent)' : 'var(--color-surface-2)',
            color:      input.trim() && !thinking ? 'var(--color-bg)' : 'var(--color-text-dim)',
            border: 'none',
            cursor: input.trim() && !thinking ? 'pointer' : 'default',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            boxShadow: input.trim() && !thinking ? '0 0 18px var(--color-accent-glow)' : 'none',
            WebkitTapHighlightColor: 'transparent',
          }}
        >
          <Icon name="arrow" size={16} sw={2.2}/>
        </button>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
