/* A shared stage shows current facts; past gestures belong in the timeline. */
function ActivityMembers({activity, events = [], agents = [], unitData = {}, connected,
  getName = getAgentDisplayName}) {
  const [, refreshAge] = React.useState(0);
  React.useEffect(() => {
    const timer = setInterval(() => refreshAge(value => value + 1), 10000);
    return () => clearInterval(timer);
  }, []);
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
        const recorded = ended ? [...events].reverse().find(event =>
          (event.agent_id || event.unit_id || event.sender) === id
          && (!activity?.activity_id || event.activity_id === activity.activity_id)
          && event.state_at_event?.status === 'current' && event.state_at_event.values) : null;
        const state = ended ? recorded?.state_at_event.values || {} : unitData[id]?.state || card?.state || {};
        const recordedAt = Number(recorded?.state_at_event.as_of);
        const member = ended ? {
          label:!recorded ? '当时状态未记录'
            : state.connected === false || state.connection_state === 'offline' ? '当时未连接'
            : activityPhysicalLabel(card, state),
          detail:Number.isFinite(recordedAt) && recordedAt > 0
            ? `记录于 ${new Date(recordedAt * 1000).toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit'})}` : '',
          color:recorded && agentKind(card || {}) === 'led' && state.ism && state.ism !== 'OFF'
            ? getLedStateInfo(state).color : null,
          stale:false, disconnected:false,
        } : activityMemberView(card, state, activity?.devices?.[id],
          connected && apiClient.devicesSynced !== false, now);
        return <div key={id} className="activity-member activity-member--companion"
          data-stale={member.stale || member.disconnected ? 'true' : undefined}>
          <span className="activity-member__portrait">
            <CompanionAvatar unitId={id} agent={card} size={96}
              mode={ended ? 'history' : 'live'}
              snapshot={ended ? {events, activity_id:activity?.activity_id} : undefined}/>
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
