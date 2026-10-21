/* Only physical device events received during this page visit are shown here. */
function useSpaceActivity(agents) {
  const { useEffect, useRef, useState } = React;
  const [recentEvents, setRecentEvents] = useState([]);
  const agentsRef = useRef(agents);
  const seenRef = useRef(new Set());
  agentsRef.current = agents;

  useEffect(() => {
    const onEvent = message => {
      const outer = message.detail || {};
      const payload = outer.payload || outer.data || outer;
      if (payload.type !== 'device' || !payload.event || typeof payload.event !== 'object') return;
      const agent = agentsRef.current.find(item => item.unit_id === payload.unit_id);
      if (!agent) return;

      const event = payload.event;
      const kind = agentKind(agent);
      let description = '';
      if (kind === 'light') {
        const levels = { DARK: '光线变暗', DIM: '光线转为微亮', NORMAL: '光线恢复正常', BRIGHT: '光线变强' };
        description = levels[event.level] || '';
      } else if (kind === 'ir' && typeof event.presence === 'boolean') {
        description = event.presence ? '检测到有人' : '未检测到人';
      } else if (kind === 'sound' && event.detected === true) {
        description = '检测到声音';
      }
      if (!description) return;

      const eventId = payload.event_id;
      if (eventId && seenRef.current.has(eventId)) return;
      if (eventId) {
        seenRef.current.add(eventId);
        if (seenRef.current.size > 40) seenRef.current.delete(seenRef.current.values().next().value);
      }
      setRecentEvents(previous => [{
        id: eventId || `${payload.unit_id}-${Date.now()}`,
        name: agent.name || getAgentMeta(agent).label,
        description,
        receivedAt: Date.now(),
      }, ...previous].slice(0, 3));
    };
    apiClient.addEventListener('server-event', onEvent);
    return () => apiClient.removeEventListener('server-event', onEvent);
  }, []);

  return recentEvents;
}
window.useSpaceActivity = useSpaceActivity;
