/* Live state and the last public gesture stay visibly distinct. */
function ActivityMembers({activity, events = [], agents = [], unitData = {}, connected,
  getName = getAgentDisplayName}) {
  const [, refreshAge] = React.useState(0);
  const [animation, setAnimation] = React.useState(null);
  const eventsRef = React.useRef(events);
  eventsRef.current = events;
  React.useEffect(() => {
    const timer = setInterval(() => refreshAge(value => value + 1), 10000);
    return () => clearInterval(timer);
  }, []);
  React.useEffect(() => {
    setAnimation(null);
    if (!connected || !activity?.activity_id || activity.status === 'ended') return;
    // Only a new server event received during this connection can animate.
    // REST history, remounts and reconnection snapshots always stay still.
    let connectedAt = Date.now() / 1000;
    let lastAnimatedAt = -Infinity;
    let settleTimer;
    const seen = new Set(eventsRef.current.map(event => event.event_id));
    const onOpen = () => { connectedAt = Date.now() / 1000; setAnimation(null); };
    const onEvent = message => {
      const outer = message.detail || {};
      const event = outer.payload || outer.data || outer;
      if (event.activity_id !== activity.activity_id || !event.event_id) return;
      if (seen.has(event.event_id) || eventsRef.current.some(item => item.event_id === event.event_id)) return;
      seen.add(event.event_id);
      if (seen.size > 640) seen.delete(seen.values().next().value);
      if (!Number.isFinite(event.ts) || event.ts < connectedAt || event.ts > Date.now() / 1000 + 5) return;
      if (event.stage === 'rps_result' && event.game_round && event.ts > lastAnimatedAt) {
        // Even after the bounded ID set rolls over, older events cannot replay.
        lastAnimatedAt = event.ts;
        setAnimation(event);
        clearTimeout(settleTimer);
        settleTimer = setTimeout(() => setAnimation(null), 1600);
      }
    };
    apiClient.addEventListener('events-open', onOpen);
    apiClient.addEventListener('server-event', onEvent);
    return () => {
      clearTimeout(settleTimer);
      apiClient.removeEventListener('events-open', onOpen);
      apiClient.removeEventListener('server-event', onEvent);
    };
  }, [activity?.activity_id, activity?.status, connected]);
  const now = Date.now() / 1000;
  const ids = [...new Set([...Object.keys(activity?.devices || {}), ...events.map(event => event.unit_id)])]
    .filter(id => id && id !== 'cloud_orchestrator');
  if (!ids.length) return <p className="activity-members__empty">正在确认参与的伙伴…</p>;
  const ended = activity?.status === 'ended';
  return <div className="activity-members" aria-label={ended ? '伙伴现在的状态，下方为历史记录' : '伙伴当前状态'}>
    {ended && <p className="activity-members__now">伙伴现在的状态 · 下方为活动历史</p>}
    <div className="activity-members__grid">
      {ids.map(id => {
        const card = agents.find(agent => agent.unit_id === id);
        const state = unitData[id]?.state || card?.state || {};
        const member = activityMemberView(card, state, activity?.devices?.[id],
          connected && apiClient.devicesSynced !== false, now);
        const gesture = latestAvatarPresentation(events, id, activity?.activity_id);
        const present = avatarStatePresentation(state, member);
        const liveGesture = animation && avatarEventPresentation(animation, id);
        const canAnimate = Boolean(gesture && liveGesture && !ended && !member.disconnected && !member.stale &&
          gesture.eventId === liveGesture.eventId);
        return <button type="button" key={id} className="activity-member activity-member--companion"
          data-stale={member.stale || member.disconnected ? 'true' : undefined}
          onClick={() => navigate('#/devices/' + encodeURIComponent(id))}>
          <span className="activity-member__heading"><strong>{getName(id)}</strong><Icon name="arrow" size={13}/></span>
          <span className="activity-member__portrait">
            <ShapeFace agent={card || id} shape={canAnimate ? liveGesture.shape : present.shape}
              expression={canAnimate ? liveGesture.expression : present.expression} size={72}
              motion={canAnimate ? liveGesture.motion : 'none'} animationKey={canAnimate ? liveGesture.eventId : null}/>
          </span>
          {present.label && <small className="activity-member__life">{present.label}</small>}
          {gesture && <span className="activity-member__gesture">最近出拳 · {gesture.label}</span>}
          <span className="activity-member__state">
            {member.color && <i className="activity-member__color" style={{background:member.color}} aria-hidden="true"/>}
            {member.label}
          </span>
          <small>{member.detail}</small>
          {member.previous && <small className="activity-member__previous">上次：{member.previous}</small>}
        </button>;
      })}
    </div>
  </div>;
}
window.ActivityMembers = ActivityMembers;
