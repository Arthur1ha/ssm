/* useSsmCore — V2 复用的通信层：连接 + 设备注册表 + ISM 实时快照 */
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
      const all = registry.getAll().filter(a =>
        a.agent_type && a.agent_type !== 'decision' && a.hw_platform !== 'cloud');
      setAgents(all);
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
    mqttBus.connect(BROKER_URL, null, { username: BROKER_USER, password: BROKER_PASS });

    fetch('/api/devices').then(r => r.json()).then(ds => {
      setAgents(prev => prev.length ? prev : ds.map(d => ({ ...d, _online: d.online ?? false })));
    }).catch(() => {});

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
