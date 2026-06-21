/* useSsmCore — V2 复用的通信层：连接 + 设备注册表 + ISM 实时快照 */
const _EXCL_TYPES = new Set(['decision']);
const _EXCL_PLAT  = new Set(['cloud']);

function useSsmCore() {
  const { useState, useEffect, useRef, useCallback } = React;
  const [connected, setConnected] = useState(false);
  const [agents, setAgents]       = useState([]);
  const [discoveredAgents, setDiscoveredAgents] = useState([]);
  const [loadingAgents, setLoadingAgents] = useState(true);
  const [unitData, setUnitData]   = useState({});
  const trackerRef = useRef(null);
  const adoptedUnitIdsRef = useRef(new Set());

  const normalizeDevices = useCallback(ds => ds
    .filter(d => d.agent_type && !_EXCL_TYPES.has(d.agent_type) && !_EXCL_PLAT.has(d.hw_platform))
    .map(d => ({ ...d, _online: d.online ?? d._online ?? false })), []);

  const refreshAgents = useCallback(() => {
    setLoadingAgents(true);
    return fetch('/api/devices?scope=adopted')
    .then(r => r.json())
    .then(ds => {
      const list = normalizeDevices(ds);
      adoptedUnitIdsRef.current = new Set(list.map(d => d.unit_id));
      setAgents(list);
      setLoadingAgents(false);
      return list;
    })
    .catch(() => {
      adoptedUnitIdsRef.current = new Set();
      setAgents([]);
      setLoadingAgents(false);
      return [];
    });
  }, [normalizeDevices]);

  const adoptLocalCandidate = useCallback(candidate => {
    const cards = normalizeDevices(candidate?.cards || []);
    if (!cards.length) return;
    adoptedUnitIdsRef.current = new Set([
      ...adoptedUnitIdsRef.current,
      ...cards.map(card => card.unit_id),
    ]);
    setAgents(prev => {
      const byId = new Map(prev.map(card => [card.unit_id, card]));
      cards.forEach(card => byId.set(card.unit_id, { ...byId.get(card.unit_id), ...card }));
      return [...byId.values()];
    });
  }, [normalizeDevices]);

  useEffect(() => {
    const registry   = new AgentRegistry(mqttBus);
    const ismTracker = new ISMTracker(mqttBus);
    trackerRef.current = ismTracker;

    const onReg = () => {
      const all = registry.getAll();
      const byId = new Map();
      const discovered = [];
      for (const a of all) {
        if (a.agent_type && !_EXCL_TYPES.has(a.agent_type) && !_EXCL_PLAT.has(a.hw_platform)) {
          discovered.push(a);
          if (adoptedUnitIdsRef.current.has(a.unit_id)) byId.set(a.unit_id, a);
        }
      }
      setDiscoveredAgents(discovered);
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
        return merged.filter(d => adoptedUnitIdsRef.current.has(d.unit_id));
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

    refreshAgents();

    return () => {
      registry.removeEventListener('change', onReg);
      ismTracker.removeEventListener('update', onIsm);
      mqttBus.removeEventListener('connect', onConn);
      mqttBus.removeEventListener('disconnect', onDisc);
    };
  }, []);

  return { connected, agents, discoveredAgents, loadingAgents, unitData, refreshAgents, adoptLocalCandidate };
}
window.useSsmCore = useSsmCore;
