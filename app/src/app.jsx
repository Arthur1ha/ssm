/* SSM PWA — App 根组件：单主屏 + Hash 路由全屏设备页 */
const { useState, useEffect, useRef } = React;

// LIME 来自 config.js 全局，此处不重复声明
const EXCL_TYPES = new Set(['decision']);
const EXCL_PLAT  = new Set(['cloud']);
const SUGGESTIONS = ['我要工作了', '帮我营造睡眠氛围', '有人来了', '我要离开了'];

const GO2_STATIC_DEVICE = {
  unit_id: 'go2',
  name: 'Go2 Air', agent_type: 'robot', _online: false,
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
  const [agents, setAgents]         = useState([]);
  const [unitData, setUnitData]     = useState({});
  const [activityLog, setActivityLog] = useState([]);
  const [rulesOpen, setRulesOpen]   = useState(false);
  const [pendingRule, setPendingRule] = useState(null);
  const [savingRule, setSavingRule]  = useState(false);

  const { thinking, thinkingText, send } = useSendIntent();
  const currentHash = useHash();
  const agentsRef     = useRef([]);
  const prevStatesRef = useRef({});
  const greetedRef    = useRef(false);
  const thinkingRef   = useRef(false);
  const pendingChipsRef = useRef([]);

  useEffect(() => { agentsRef.current = agents; }, [agents]);

  const appendActivity = (entry) =>
    setActivityLog(prev => [...prev.slice(-19), { ...entry, ts: Date.now() }]);

  /* 命令进行中（thinking）期间到达的设备状态变化先缓存，待这一轮的
     管家/灯气泡落地后再追加，保证灰色状态条排在气泡之后而非抢到前面。 */
  useEffect(() => {
    thinkingRef.current = thinking;
    if (!thinking && pendingChipsRef.current.length) {
      const chips = pendingChipsRef.current;
      pendingChipsRef.current = [];
      chips.forEach(text => appendActivity({ type: 'event', text }));
    }
  }, [thinking]);

  /* ── REST 预载：立即获取已知设备，不等 MQTT ── */
  useEffect(() => {
    fetch('/api/devices')
      .then(r => r.json())
      .then(devices => {
        const restAgents = devices
          .filter(d => d.agent_type && !EXCL_TYPES.has(d.agent_type) && !EXCL_PLAT.has(d.hw_platform))
          .map(d => ({ ...d, _online: d.online ?? false }));
        const hasGo2 = restAgents.some(d => d.unit_id === 'go2');
        setAgents(hasGo2 ? restAgents : [GO2_STATIC_DEVICE, ...restAgents]);
      })
      .catch(() => setAgents([GO2_STATIC_DEVICE]));
  }, []);

  /* ── MQTT 初期化 ── */
  useEffect(() => {
    const registry   = new AgentRegistry(mqttBus);
    const ismTracker = new ISMTracker(mqttBus);

    const handleRegistryChange = () => {
      const all = registry.getAll();
      // unit_id 为统一索引（REST 和 MQTT manifest 现在都用真实 unit_id）
      const mqttByUnitId = new Map();
      for (const a of all) {
        const id = a.unit_id;
        if (a.agent_type && !EXCL_TYPES.has(a.agent_type) && !EXCL_PLAT.has(a.hw_platform)) {
          mqttByUnitId.set(id, a);
        }
      }
      setAgents(prev => {
        const merged = prev.map(d => {
          const id = d.unit_id;
          const mqtt = mqttByUnitId.get(id);
          if (mqtt) {
            mqttByUnitId.delete(id);
            if (id === 'go2') return { ...GO2_STATIC_DEVICE, _online: mqtt._online === true };
            return { ...d, ...mqtt };
          }
          // go2 只发 card+status、不发 manifest，故没 agent_type、被上面的过滤丢掉，
          // 不会进 mqttByUnitId。但它的在线状态仍要实时同步到已有卡片，
          // 否则上线/掉线后卡片不刷新（必须手动刷 PWA）。
          const reg = registry.get(id);
          if (reg && reg._online !== undefined && (reg._online === true) !== (d._online === true)) {
            return { ...d, _online: reg._online === true };
          }
          return d;
        });
        // 追加 MQTT 报告但 REST 列表里没有的新设备
        for (const a of mqttByUnitId.values()) merged.push(a);
        return merged;
      });
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
              const agent = agentsRef.current.find(a => (a.unit_id) === uid);
              const name  = agent?.name || uid;
              const text  = `${name} → ${ism}`;
              /* turn 进行中先缓存，turn 结束（thinking→false）后由上面的 effect 追加 */
              if (thinkingRef.current) pendingChipsRef.current.push(text);
              else appendActivity({ type: 'event', text });
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
      mqttBus.subscribe('ssm/agents/esp32_desk_led/speech');
      mqttBus.subscribe('ssm/agents/+/thought');
      if (!greetedRef.current) {
        greetedRef.current = true;
        appendActivity({ type: 'ai', agent: 'orchestrator', text: '你好呀，我是智慧空间管家，有什么可以帮你的嘛？' });
      }
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
    mqttBus.addEventListener('topic:ssm/agents/esp32_desk_led/speech', handleSpeech);

    /* 多智能体 thought 气泡 */
    const handleThought = (e) => {
      const { topic, msg } = e.detail || {};
      if (!topic || !msg) return;
      const parts = topic.split('/');
      // 匹配 ssm/agents/{agentId}/thought
      if (parts.length !== 4 || parts[0] !== 'ssm' || parts[1] !== 'agents' || parts[3] !== 'thought') return;
      const { text, type } = msg;
      if (!text || type === 'act') return;   // act 类型是执行日志，不展示
      appendActivity({ type: 'ai', agent: parts[2], text });
    };
    mqttBus.addEventListener('message', handleThought);

    return () => {
      registry.removeEventListener('change', handleRegistryChange);
      ismTracker.removeEventListener('update', handleIsmUpdate);
      mqttBus.removeEventListener('connect',    handleConnect);
      mqttBus.removeEventListener('disconnect', handleDisconnect);
      mqttBus.removeEventListener('reconnect',  handleReconnect);
      mqttBus.removeEventListener('topic:ssm/agents/esp32_desk_led/speech', handleSpeech);
      mqttBus.removeEventListener('message', handleThought);
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
      appendActivity({ type: 'ai', agent: 'orchestrator', text: `规则「${saved.name}」已保存，条件触发时自动执行。` });
    } catch {
      appendActivity({ type: 'ai', agent: 'orchestrator', text: '规则保存失败，请重试。' });
    }
    setSavingRule(false);
  };

  const handleCancelRule = () => {
    setPendingRule(null);
    appendActivity({ type: 'ai', agent: 'orchestrator', text: '已取消，规则未保存。' });
  };

  /* ── 主屏发送 ── */
  const handleSend = (text) => {
    const t = text.trim();
    if (!t) return;
    appendActivity({ type: 'user', text: t });

    send(t, {
      onMessage:     (msg)  => appendActivity({ type: 'ai', agent: 'orchestrator', text: msg }),
      onPendingRule: (rule) => setPendingRule(rule),
    });
  };

  /* ── Hash 路由：全屏设备页 ── */
  const hashMatch = currentHash.match(/^#\/devices\/([^/]+)$/);
  if (hashMatch) {
    const routeId = hashMatch[1];
    if (routeId === 'go2') {
      return <Go2DevicePage onBack={() => navigate('#')}/>;
    }
    const device = agents.find(a => a.unit_id === routeId);
    if (!device) { navigate('#'); return null; }
    return (
      <DeviceDetailPage
        unitId={routeId}
        device={device}
        unitData={unitData}
        onBack={() => navigate('#')}
      />
    );
  }

  /* ── 主屏布局 ── */
  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'var(--bg-gradient)', color: '#fff',
      fontFamily: 'var(--font-sans)',
      paddingTop: 'env(safe-area-inset-top, 0px)',
      display: 'flex', flexDirection: 'column',
    }}>

      {/* 头部 */}
      <div style={{
        padding: '12px 16px', display: 'flex',
        alignItems: 'center', justifyContent: 'space-between', flexShrink: 0,
        borderBottom: '1px solid var(--color-border)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 17, fontWeight: 700, letterSpacing: '0.08em', fontFamily: 'var(--font-sans)' }}>SSM</span>
          <div
            onClick={!connected ? () => mqttBus.connect(BROKER_URL, null, { username: BROKER_USER, password: BROKER_PASS }) : undefined}
            style={{
              width: 7, height: 7, borderRadius: '50%',
              background: connected ? 'var(--color-accent)' : 'var(--color-danger)',
              boxShadow: connected ? '0 0 8px var(--color-online-glow)' : 'none',
              cursor: connected ? 'default' : 'pointer',
            }}
          />
        </div>
        <button onClick={() => setRulesOpen(true)} className="btn">
          <Icon name="list" size={13}/>
          规则
        </button>
      </div>

      {/* 可滚动主体 */}
      <div style={{ flex: 1, overflowY: 'auto' }}>

        {/* 设备分组 */}
        <DevicesScreen agents={agents} unitData={unitData}/>

        {/* 活动分隔线 */}
        {activityLog.length > 0 && (
          <div style={{ padding: '16px 20px 8px', marginTop: 4 }}>
            <span style={{ fontSize: 13, color: 'var(--color-text-dim)', fontWeight: 500 }}>活动</span>
          </div>
        )}

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
                当 {pendingRule.trigger?.tag}.{pendingRule.trigger?.event}
                {' → '}
                {pendingRule.action?.tag} / {pendingRule.action?.cmd}
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
      <div style={{ flexShrink: 0, background: 'var(--color-bar)', backdropFilter: 'var(--glass-blur)', WebkitBackdropFilter: 'var(--glass-blur)', borderTop: '1px solid var(--color-border)' }}>
        {activityLog.every(e => e.type !== 'user') && !thinking && (
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
      background: 'transparent',
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
