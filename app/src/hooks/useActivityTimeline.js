/* Restore a live space activity after reload. Keep ended activity cards only
 * when this page has already seen them active, so they stay with its dialogue.
 */
function useActivityTimeline() {
  const { useEffect, useRef, useState, useCallback } = React;
  const [activity, setActivity] = useState(null);
  const [displayActivity, setDisplayActivity] = useState(null);
  const [events, setEvents] = useState([]);
  const [ending, setEnding] = useState(false);
  const [error, setError] = useState('');
  const activityIdRef = useRef(null);
  const seenActiveIdsRef = useRef(new Set());
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
      if (current?.activity_id) seenActiveIdsRef.current.add(current.activity_id);
      const latestSeen = data.latest_activity_id &&
        seenActiveIdsRef.current.has(data.latest_activity_id);
      const priorSeen = activityIdRef.current &&
        seenActiveIdsRef.current.has(activityIdRef.current);
      const id = current?.activity_id ||
        (latestSeen ? data.latest_activity_id : priorSeen ? activityIdRef.current : null);
      setActivity(current);
      setError('');
      if (activityIdRef.current !== id) {
        activityIdRef.current = id;
        setEvents([]);
        setDisplayActivity(current);
      } else if (current) {
        setDisplayActivity(current);
      } else {
        setDisplayActivity(previous => previous?.activity_id === id
          ? (previous.status === 'ended' ? previous : { ...previous, status: 'ended' }) : null);
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
        refresh();
        return;
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

  return { activity, displayActivity, events, ending, error, endActivity };
}
window.useActivityTimeline = useActivityTimeline;
