/* useSsmCore — read-only device snapshot + authenticated server events. */
const _EXCL_TYPES = new Set(['decision']);
const _EXCL_PLAT = new Set(['cloud']);

function mergeDeviceState(previous, unitId, state) {
  if (!state || typeof state !== 'object' || !Object.keys(state).length) return previous;
  const current = previous[unitId]?.state;
  if (current && (current.ts || 0) > (state.ts || 0)) return previous;
  return { ...previous, [unitId]: { ...(previous[unitId] || {}), state } };
}

function useSsmCore() {
  const { useState, useEffect, useRef, useCallback } = React;
  const [connected, setConnected] = useState(apiClient.eventsConnected);
  const [agents, setAgents] = useState([]);
  const [loadingAgents, setLoadingAgents] = useState(true);
  const [agentsError, setAgentsError] = useState('');
  const [unitData, setUnitData] = useState({});
  const refreshSeqRef = useRef(0);

  const normalizeDevices = useCallback(devices => (Array.isArray(devices) ? devices : [])
    .filter(device => device.agent_type && !_EXCL_TYPES.has(device.agent_type) && !_EXCL_PLAT.has(device.hw_platform))
    .map(device => ({ ...device, _online: device.online ?? device._online ?? false })), []);

  const refreshAgents = useCallback(async () => {
    const seq = ++refreshSeqRef.current;
    setLoadingAgents(true);
    setAgentsError('');
    try {
      const devices = await apiClient.request('/api/devices');
      if (seq !== refreshSeqRef.current) return [];
      const list = normalizeDevices(devices);
      setAgents(list);
      setUnitData(previous => list.reduce(
        (data, device) => mergeDeviceState(data, device.unit_id, device.state), previous
      ));
      return list;
    } catch (error) {
      if (seq !== refreshSeqRef.current) return [];
      setAgentsError(apiErrorText(error, '设备服务暂时不可用，请重试'));
      return [];
    } finally {
      if (seq === refreshSeqRef.current) setLoadingAgents(false);
    }
  }, [normalizeDevices]);

  useEffect(() => { refreshAgents(); }, [refreshAgents]);

  useEffect(() => {
    const connectedHandler = () => setConnected(true);
    const disconnectedHandler = () => setConnected(false);
    const serverEventHandler = event => {
      const outer = event.detail || {};
      const payload = outer.payload || outer.data || outer;
      const kind = outer.type || payload.type || outer.event;
      if (!['device', 'card', 'status', 'state', 'event'].includes(kind)) return;
      const unitId = payload.unit_id || payload.agent_id;
      if (!unitId) return;
      const isCardUpdate = Object.prototype.hasOwnProperty.call(payload, 'card');
      if (isCardUpdate) {
        ++refreshSeqRef.current;
        setLoadingAgents(false);
      }
      setAgents(previous => {
        const card = payload.card && typeof payload.card === 'object' ? payload.card : null;
        if (isCardUpdate) {
          const kept = previous.filter(device => device.unit_id !== unitId
            && !(card && device.unit_id === card.device_id && card.unit_id !== card.device_id));
          return card ? [...kept, { ...card, _online: payload.online ?? card.online ?? false }] : kept;
        }
        return previous.map(device => device.unit_id === unitId || device.device_id === unitId || device.parent_id === unitId
          ? { ...device, _online: payload.online ?? device._online }
          : device);
      });
      if (isCardUpdate) {
        setUnitData(previous => payload.card
          ? { ...previous, [unitId]: { ...(previous[unitId] || {}), state: payload.card.state || {} } }
          : Object.fromEntries(Object.entries(previous).filter(([id]) => id !== unitId)));
        refreshAgents();
      }
      if (payload.state || payload.event) {
        setUnitData(previous => {
          const withState = payload.state ? mergeDeviceState(previous, unitId, payload.state) : previous;
          return payload.event
            ? { ...withState, [unitId]: { ...(withState[unitId] || {}), event: payload.event } }
            : withState;
        });
      }
    };
    apiClient.addEventListener('events-open', connectedHandler);
    apiClient.addEventListener('events-error', disconnectedHandler);
    apiClient.addEventListener('server-event', serverEventHandler);
    apiClient.openEvents();
    return () => {
      apiClient.removeEventListener('events-open', connectedHandler);
      apiClient.removeEventListener('events-error', disconnectedHandler);
      apiClient.removeEventListener('server-event', serverEventHandler);
    };
  }, [refreshAgents]);

  return { connected, agents, loadingAgents, agentsError, unitData, refreshAgents };
}
window.useSsmCore = useSsmCore;
