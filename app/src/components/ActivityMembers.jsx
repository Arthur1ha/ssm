/* A shared stage shows current facts; past gestures belong in the timeline. */
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
  if (!ids.length) return null;
  const ended = activity?.status === 'ended';
  const joined = ids.filter(id => activity?.devices?.[id]?.status === 'joined').length;
  const participation = ended ? '' : activity?.status === 'ending' ? '正在结束活动'
    : joined ? `${joined} 位伙伴参与活动` : '';
  return <div className="activity-members" aria-label="伙伴当前状态">
    <div className="activity-members__grid">
      {ids.map(id => {
        const card = agents.find(agent => agent.unit_id === id);
        const state = unitData[id]?.state || card?.state || {};
        const member = activityMemberView(card, state, activity?.devices?.[id],
          connected && apiClient.devicesSynced !== false, now);
        const present = avatarStatePresentation(state, member);
        const liveGesture = live[id]?.gesture;
        const liveEmotion = live[id]?.emotion;
        const available = !member.disconnected && !member.stale;
        const canAnimate = Boolean(liveGesture && !ended && available);
        const emotion = canAnimate ? liveGesture.expression : available && liveEmotion ? liveEmotion.value : present.expression;
        return <div key={id} className="activity-member activity-member--companion"
          data-stale={member.stale || member.disconnected ? 'true' : undefined}>
          <span className="activity-member__portrait">
            <ShapeFace agent={card || id} shape={canAnimate ? liveGesture.shape : present.shape}
              emotion={emotion} behavior={present.behavior} size={96} animated={available && !ended}
              online={!connected || apiClient.devicesSynced === false ? null
                : member.disconnected ? false : member.stale ? null : true}
              motion={canAnimate ? liveGesture.motion : undefined}
              animationKey={canAnimate ? liveGesture.eventId : liveEmotion?.eventId}/>
          </span>
          <span className="activity-member__heading"><strong>{getName(id)}</strong></span>
          <span className="activity-member__state">
            {member.color && <i className="activity-member__color" style={{background:member.color}} aria-hidden="true"/>}
            {member.label}
          </span>
          {member.detail && <small>{member.detail}</small>}
          {member.previous && <small className="activity-member__previous">上次：{member.previous}</small>}
        </div>;
      })}
    </div>
    {participation && <p className="activity-members__footer">{participation}</p>}
  </div>;
}
window.ActivityMembers = ActivityMembers;
