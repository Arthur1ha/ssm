/* Live state and the last public gesture stay visibly distinct. */
function ActivityMembers({activity, events = [], agents = [], unitData = {}, connected,
  getName = getAgentDisplayName}) {
  const [, refreshAge] = React.useState(0);
  React.useEffect(() => {
    const timer = setInterval(() => refreshAge(value => value + 1), 10000);
    return () => clearInterval(timer);
  }, []);
  const live = useAvatarPresentation({events, activityId:activity?.activity_id,
    active:Boolean(activity && !['ending','ended'].includes(activity.status)), connected});
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
        const liveGesture = live[id]?.gesture;
        const liveEmotion = live[id]?.emotion;
        const available = !member.disconnected && !member.stale;
        const canAnimate = Boolean(liveGesture && !ended && available);
        const emotion = canAnimate ? liveGesture.expression : available && liveEmotion ? liveEmotion.value : present.expression;
        return <button type="button" key={id} className="activity-member activity-member--companion"
          data-stale={member.stale || member.disconnected ? 'true' : undefined}
          onClick={() => navigate('#/devices/' + encodeURIComponent(id))}>
          <span className="activity-member__heading"><strong>{getName(id)}</strong><Icon name="arrow" size={13}/></span>
          <span className="activity-member__portrait">
            <ShapeFace agent={card || id} shape={canAnimate ? liveGesture.shape : present.shape}
              emotion={emotion} behavior={present.behavior} size={72} animated={available && !ended}
              motion={canAnimate ? liveGesture.motion : undefined}
              animationKey={canAnimate ? liveGesture.eventId : liveEmotion?.eventId}/>
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
