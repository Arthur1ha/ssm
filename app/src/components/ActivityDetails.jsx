/* Live device facts stay separate from the historical evidence of one action. */
function ActivityMembers({activity, events = [], agents = [], unitData = {}, connected, getName = getAgentDisplayName}) {
  const [now, setNow] = React.useState(() => Date.now() / 1000);
  React.useEffect(() => {
    // Age the displayed feedback without polling the device or API.
    const timer = setInterval(() => setNow(Date.now() / 1000), 10000);
    return () => clearInterval(timer);
  }, []);
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
        return <button type="button" key={id} className="activity-member"
          data-stale={member.stale || member.disconnected ? 'true' : undefined}
          onClick={() => navigate('#/devices/' + encodeURIComponent(id))}>
          <span className="activity-member__heading"><strong>{getName(id)}</strong><Icon name="arrow" size={13}/></span>
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

function ActivityEvidenceDrawer({event, events, agents, getName, onClose}) {
  const dialogRef = React.useRef(null);
  const closeRef = React.useRef(onClose);
  closeRef.current = onClose;
  React.useEffect(() => {
    const previous = document.activeElement;
    dialogRef.current?.querySelector('button')?.focus();
    const keydown = e => {
      if (e.key === 'Escape') { e.preventDefault(); closeRef.current(); }
      if (e.key !== 'Tab') return;
      const controls = [...dialogRef.current.querySelectorAll('button, a[href], summary')];
      const first = controls[0], last = controls[controls.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener('keydown', keydown);
    return () => {
      document.removeEventListener('keydown', keydown);
      if (previous?.isConnected) previous.focus();
    };
  }, []);
  const related = activityRelatedEvents(event, events);
  const task = related.task.length ? related.task : [event];
  const reason = [...task].reverse().map(activityReasonText).find(Boolean);
  const before = task.find(item => item.event_type === 'action' && item.state_at_event);
  const after = [...task].reverse().find(item => item.event_type === 'action_result' && item.state_at_event);
  const observation = task.find(item => item.decision_observation)?.decision_observation;
  const card = agents.find(agent => agent.unit_id === event.unit_id);
  const describeSnapshot = item => {
    const snapshot = item?.state_at_event;
    if (!snapshot || snapshot.status !== 'current') return '当时没有新鲜的状态反馈';
    return activityPhysicalLabel(card, snapshot.values);
  };
  const time = ts => ts ? new Date(ts * 1000).toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '';
  return ReactDOM.createPortal(<div className="activity-evidence-layer">
    <div className="activity-evidence-mask" onClick={onClose}/>
    <section className="activity-evidence" ref={dialogRef} role="dialog" aria-modal="true"
      aria-labelledby="activity-evidence-title">
      <header><div><small>{getName(event.unit_id)} · {time(event.ts)}</small>
        <h2 id="activity-evidence-title">{event.event_type === 'message' ? '这条消息的关联进展' : '这次行动的依据'}</h2></div>
        <button type="button" className="icon-button" onClick={onClose} aria-label="关闭变化依据"><Icon name="x" size={18}/></button>
      </header>
      <div className="activity-evidence__body">
        <section><h3>关联的消息</h3>
          {related.messages.length ? related.messages.map(message => <blockquote key={message.event_id}>
            <small>{getName(message.unit_id)}{message.target_unit_id ? ` → ${getName(message.target_unit_id)}` : ''} · {time(message.ts)}</small>
            <p>{message.text}</p>
          </blockquote>) : <p className="activity-evidence__missing">这次记录没有关联触发消息，无法确认由哪条对话或观察引起。</p>}
        </section>
        {task.some(item => item.event_type !== 'message') && <section><h3>执行与反馈</h3>
          <ol>{task.filter(item => item.event_type !== 'message').map(item => <li key={item.event_id}>
            <time>{time(item.ts)}</time><div>{activityStatusText(item, '', true)}
              {item.event_type === 'action_result' && /[\u3400-\u9fff]/.test(item.text || '')
                && item.text !== reason
                && item.text !== activityStatusText(item, '', true) && <p>{item.text}</p>}</div>
          </li>)}</ol>
        </section>}
        {event.event_type === 'message' && !related.task.length && <section><h3>设备动作</h3>
          <p className="activity-evidence__missing">这条消息还没有关联的动作记录，不能据此确认设备已经行动。</p>
        </section>}
        {reason && <section><h3>限制与原因</h3><p>{reason}</p></section>}
        {event.event_type !== 'message' && <section><h3>记录时的设备反馈</h3>
          {before || after ? <dl>
            {before && <><dt>开始时 · {time(before.state_at_event.as_of)}</dt><dd>{describeSnapshot(before)}</dd></>}
            {after && <><dt>结果时 · {time(after.state_at_event.as_of)}</dt><dd>{describeSnapshot(after)}</dd></>}
          </dl> : <p className="activity-evidence__missing">这次记录没有保存动作前后的状态反馈。</p>}
          <small className="activity-evidence__caption">这里是当时收到的设备反馈；伙伴现在的状态请看活动顶部。</small>
        </section>}
        {observation && <section><h3>这次判断参考的感知</h3><dl>
          {observation.light_level && <><dt>环境光线</dt><dd>{({DARK:'暗',DIM:'微亮',NORMAL:'正常',BRIGHT:'明亮',UNKNOWN:'未确认'})[observation.light_level] || '未确认'}</dd></>}
          {typeof observation.sound_detected === 'boolean' && <><dt>近期声音</dt><dd>{observation.sound_detected ? '检测到声音' : '未检测到声音'}</dd></>}
          {observation.time_period && <><dt>时间段</dt><dd>{observation.time_period}</dd></>}
        </dl></section>}
      </div>
    </section>
  </div>, document.body);
}
Object.assign(window, {ActivityMembers, ActivityEvidenceDrawer});
