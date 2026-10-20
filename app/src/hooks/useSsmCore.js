/* useSsmCore — read-only device snapshot + authenticated server events. */
const _EXCL_TYPES = new Set(['decision']);
const _EXCL_PLAT = new Set(['cloud']);

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
      setAgents(previous => {
        const card = payload.card && typeof payload.card === 'object' ? payload.card : null;
        let found = false;
        const merged = previous
          .filter(device => !(card && device.unit_id === card.device_id && card.unit_id !== card.device_id))
          .map(device => {
            if (device.unit_id !== unitId) return device;
            found = true;
            return {
              ...device,
              ...(card || {}),
              _online: payload.online ?? card?.online ?? device._online,
            };
          });
        if (!found && card?.unit_id) {
          merged.push({ ...card, _online: payload.online ?? card.online ?? false });
        }
        return merged;
      });
      if (payload.state || payload.event) {
        setUnitData(previous => ({
          ...previous,
          [unitId]: {
            ...(previous[unitId] || {}),
            ...(payload.state ? { state: payload.state } : {}),
            ...(payload.event ? { event: payload.event } : {}),
          },
        }));
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
  }, []);

  return { connected, agents, loadingAgents, agentsError, unitData, refreshAgents };
}
window.useSsmCore = useSsmCore;
