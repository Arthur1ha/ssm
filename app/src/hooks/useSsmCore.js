/* useSsmCore — V2 复用的通信层：连接 + 设备注册表 + ISM 实时快照 */
const _EXCL_TYPES = new Set(['decision']);
const _EXCL_PLAT  = new Set(['cloud']);
const _GO2_STATIC = {
  unit_id: 'go2', name: 'Go2 Air', agent_type: 'robot', _online: false,
  capabilities: ['MOVE', 'STAND_UP', 'SIT_DOWN', 'HELLO', 'STRETCH', 'DANCE'],
};

function useSsmCore() {
  const { useState, useEffect, useRef } = React;
  const [connected, setConnected] = useState(false);
  const [agents, setAgents]       = useState([]);
  const [unitData, setUnitData]   = useState({});
  const trackerRef = useRef(null);

  useEffect(() => {
    const registry   = new AgentRegistry(mqttBus);
    const ismTracker = new ISMTracker(mqttBus);
    trackerRef.current = ismTracker;

    const onReg = () => {
      const all = registry.getAll();
      const byId = new Map();
      for (const a of all) {
        if (a.agent_type && !_EXCL_TYPES.has(a.agent_type) && !_EXCL_PLAT.has(a.hw_platform)) {
          byId.set(a.unit_id, a);
        }
      }
      setAgents(prev => {
        const merged = prev.map(d => {
          const mqtt = byId.get(d.unit_id);
          if (mqtt) { byId.delete(d.unit_id); return { ...d, ...mqtt }; }
          // go2 只发 card 不发 manifest，在线状态从 registry 直接同步
          const reg = registry.get(d.unit_id);
          if (reg && reg._online !== undefined) return { ...d, _online: reg._online === true };
          return d;
        });
        for (const a of byId.values()) merged.push(a);
        return merged;
      });
    };
    registry.addEventListener('change', onReg);

    let pending = false;
    const onIsm = () => {
      if (pending) return; pending = true;
      requestAnimationFrame(() => {
        pending = false;
        const snap = {};
        ismTracker.unitIds().forEach(uid => {
          snap[uid] = { state: ismTracker.get(uid, 'state'), event: ismTracker.get(uid, 'event') };
        });
        setUnitData(snap);
      });
    };
    ismTracker.addEventListener('update', onIsm);

    const onConn = () => { setConnected(true); mqttBus.subscribe('ssm/agents/+/thought'); };
    const onDisc = () => setConnected(false);
    mqttBus.addEventListener('connect', onConn);
    mqttBus.addEventListener('disconnect', onDisc);
    if (!mqttBus._client) mqttBus.connect(BROKER_URL, null, { username: BROKER_USER, password: BROKER_PASS });

    // REST 预载：兜底保证 Go2 卡片始终显示（与 V1 app.jsx 一致）
    fetch('/api/devices').then(r => r.json()).then(ds => {
      const list = ds
        .filter(d => d.agent_type && !_EXCL_TYPES.has(d.agent_type) && !_EXCL_PLAT.has(d.hw_platform))
        .map(d => ({ ...d, _online: d.online ?? false }));
      const hasGo2 = list.some(d => d.unit_id === 'go2');
      setAgents(prev => prev.length ? prev : (hasGo2 ? list : [_GO2_STATIC, ...list]));
    }).catch(() => setAgents(prev => prev.length ? prev : [_GO2_STATIC]));

    return () => {
      registry.removeEventListener('change', onReg);
      ismTracker.removeEventListener('update', onIsm);
      mqttBus.removeEventListener('connect', onConn);
      mqttBus.removeEventListener('disconnect', onDisc);
    };
  }, []);

  return { connected, agents, unitData };
}
window.useSsmCore = useSsmCore;
