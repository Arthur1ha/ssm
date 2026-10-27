/* A space activity outlives a browser session. Rehydrate its bounded, visible
 * history on entry and after SSE reconnect, then merge live events by event_id.
 */
function useActivityTimeline() {
  const { useEffect, useRef, useState, useCallback } = React;
  const [activity, setActivity] = useState(null);
  const [events, setEvents] = useState([]);
  const [ending, setEnding] = useState(false);
  const [error, setError] = useState('');
  const activityIdRef = useRef(null);
  const loadSeq = useRef(0);

  const mergeEvents = useCallback(incoming => {
    if (!Array.isArray(incoming)) return;
    setEvents(previous => {
      const byId = new Map();
      [...previous, ...incoming].forEach(item => {
        if (item && item.event_id && item.activity_id === activityIdRef.current) {
          byId.set(item.event_id, item);
        }
      });
      return [...byId.values()].sort((a, b) =>
        (Number(a.ts) || 0) - (Number(b.ts) || 0) ||
        String(a.event_id).localeCompare(String(b.event_id))).slice(-160);
    });
  }, []);

  const loadEvents = useCallback(async id => {
    const data = await apiClient.request(`/api/activity/${encodeURIComponent(id)}/events`);
    if (activityIdRef.current === id) mergeEvents(data.events);
  }, [mergeEvents]);

  const refresh = useCallback(async () => {
    const seq = ++loadSeq.current;
    try {
      const data = await apiClient.request('/api/activity/current');
      if (seq !== loadSeq.current) return;
      const current = data.activity || null;
      const id = current?.activity_id || data.latest_activity_id || null;
      setActivity(current);
      setError('');
      if (activityIdRef.current !== id) {
        activityIdRef.current = id;
        setEvents([]);
      }
      if (id) await loadEvents(id);
    } catch (requestError) {
      if (seq === loadSeq.current && requestError.status !== 401) {
        setError(apiErrorText(requestError, '活动记录暂时无法读取'));
      }
    }
  }, [loadEvents]);

  useEffect(() => {
    const onServerEvent = message => {
      const outer = message.detail || {};
      const item = outer.payload || outer.data || outer;
      if (item.type !== 'activity' || !item.event_id || !item.activity_id) return;
      if (activityIdRef.current !== item.activity_id) {
        activityIdRef.current = item.activity_id;
        setEvents([]);
        loadEvents(item.activity_id).catch(() => {});
      }
      mergeEvents([item]);
      if (['started', 'ended', 'device_joined', 'device_left', 'device_failed',
        'activity_started', 'activity_ended', 'mode_changed'].includes(item.event_type)) {
        refresh();
      }
    };
    apiClient.addEventListener('server-event', onServerEvent);
    apiClient.addEventListener('events-open', refresh);
    refresh();
    return () => {
      apiClient.removeEventListener('server-event', onServerEvent);
      apiClient.removeEventListener('events-open', refresh);
    };
  }, [loadEvents, mergeEvents, refresh]);

  const endActivity = useCallback(async () => {
    if (!activity || ending) return;
    setEnding(true);
    setError('');
    try {
      await apiClient.request('/api/activity/end', { method: 'POST', body: {} });
      await refresh();
    } catch (requestError) {
      setError(apiErrorText(requestError, '结束活动失败，请重试'));
    } finally {
      setEnding(false);
    }
  }, [activity, ending, refresh]);

  return { activity, events, ending, error, endActivity };
}
window.useActivityTimeline = useActivityTimeline;
