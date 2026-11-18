/* Space activity placement; conversation rendering lives in ChatMessages. */
function ActivityFeed({ entries, agents = [], unitData = {}, connected = false, activity, activityEvents = [], activityHistory = [], activityEnding, activityError,
  onEndActivity, onAnswerActivity, training, onReviseRule, busy = false }) {
  const getName = unitId => getAgentDisplayName(agents.find(agent => agent.unit_id === unitId) || unitId);
  const hasActivity = Boolean(activity || activityEvents.length || activityError);
  const currentCard = hasActivity ? {
    id: activity?.activity_id || activityEvents[0]?.activity_id || 'recent',
    activity, events: activityEvents, ending: activityEnding, error: activityError,
  } : null;
  const cards = [...activityHistory, ...(currentCard ? [currentCard] : [])];
  const findAnchor = card => {
    const started = card.events.find(event => event.event_type === 'started');
    const context = card.activity?.context || card.activity?.text || started?.text || '';
    const sourceEventId = card.activity?.source_event_id || started?.source_event_id;
    const owner = sourceEventId
      ? entries.find(entry => entry.type === 'user' && entry.intentEventId === sourceEventId)
      : null;
    const requestId = owner?.requestId || null;
    if (!requestId) return -1;
    const finalIndex = entries.findIndex(entry => entry.type === 'ai'
      && entry.requestId === requestId && entry.final);
    if (finalIndex >= 0) return finalIndex + 1;
    for (let index = entries.length - 1; index >= 0; index--) {
      if (entries[index].requestId === requestId) return index + 1;
    }
    return -1;
  };
  const cardsAt = new Map();
  cards.forEach(card => {
    const anchor = findAnchor(card);
    // Keep the steward's start confirmation above its activity card.
    // Autonomous activities stay after the chat entries.
    const position = anchor >= 0 ? anchor : card === currentCard ? entries.length : 0;
    cardsAt.set(position, [...(cardsAt.get(position) || []), card]);
  });
  const rows = [{ type: 'ai', agent: 'cloud_orchestrator',
    text: getAgentOpeningLine('cloud_orchestrator'), eventId: 'welcome' }];
  for (let index = 0; index <= entries.length; index++) {
    (cardsAt.get(index) || []).forEach(card => rows.push({ type: 'space_activity', card }));
    if (index < entries.length) rows.push(entries[index]);
  }
  return <ChatMessages entries={rows} agents={agents} training={training} onReviseRule={onReviseRule} busy={busy}
    renderEntry={e => {
      if (e.type === 'space_activity') {
        const card = e.card;
        return <div key={`activity-${card.id}`}>
          <SpaceActivityFeed
            activity={card.activity} events={card.events} ending={card.ending}
            error={card.error} onEnd={onEndActivity} onAnswer={onAnswerActivity}
            getName={getName} agents={agents} unitData={unitData} connected={connected}/>
        </div>;
      }
      return null;
    }}/>;
}
window.ActivityFeed = ActivityFeed;

function projectActivityEvents(events) {
  const seen = new Set();
  return events.filter(event => {
    if (event.display === false || event.event_type === 'started' || event.stage === 'narrated') return false;
    const identity = event.source_event_id || event.event_id;
    if (identity && seen.has(identity)) return false;
    if (activityGameEnd(event) && event.event_type !== 'game_ended'
      && events.some(other => other.event_type === 'game_ended' && other.game_id === event.game_id
        && other.trigger_message_id === event.trigger_message_id)) return false;
    if (event.event_type === 'observation' && event.observation_event_id
      && events.some(other => other.event_type === 'search_progress' && other.display !== false
        && other.observation_event_id === event.observation_event_id && other.unit_id === event.unit_id)) return false;
    if (identity) seen.add(identity);
    return Boolean(activityGameEnd(event)) || !(event.event_type === 'response' && (event.status === 'ok'
      || event.result_kind === 'message' && ['received','accepted'].includes(event.status)));
  });
}

function activityStatusText(event, name, compact = false) {
  const type = event.event_type;
  const mode = { reactive: '自主调光', free_explore: '自由探索' }[event.mode] || event.mode || '自主模式';
  if (type === 'adjustment_summary') return `${name}连续调整了灯光 ${event.count} 次`;
  if (type === 'started') return '活动已开始';
  if (type === 'ended') return '活动已结束';
  if (type === 'device_joined') return `${compact ? '' : name}已进入${mode}`;
  if (type === 'response' && event.status === 'received') return '消息已送达';
  if (type === 'response' && event.status === 'accepted') return '消息已接受';
  if (type === 'response') return `${compact ? '' : name}${event.status === 'timeout'
    ? '暂时没有收到回应' : '暂时无法回应'}`;
  if (type === 'device_left') return event.status === 'offline'
    ? `${compact ? '' : name}当前离线，无法参与活动`
    : event.status === 'left'
      ? `${compact ? '' : name}已退出自主活动${event.physical_stop === 'confirmed' ? ' · 停止已确认'
        : event.physical_stop === 'unknown' ? ' · 停止结果待确认' : ''}`
      : event.status === 'unknown'
        ? `${compact ? '' : `${name}的`}活动状态还没有确认`
      : `${compact ? '' : name}暂时无法加入${mode}`;
  return `${event.unit_id && !compact ? `${name} · ` : ''}${processUserText(event.text) || activityReasonText(event) || processOutcome(event)}`;
}

/* Speech stays on the surface; actual runs/tasks have one shared disclosure. */
function SpaceActivityFeed({activity,events,ending,error,onEnd,onAnswer,agents=[],unitData={},connected=false,
  getName=getAgentDisplayName}) {
  const [expanded,setExpanded] = React.useState(false);
  const [hasNewEvents,setHasNewEvents] = React.useState(false);
  const [latestOutside,setLatestOutside] = React.useState(false);
  const [windowStart,setWindowStart] = React.useState(null);
  const timelineRef = React.useRef(null);
  const latestRef = React.useRef(null);
  const lastArrival = React.useRef(null);
  const activityId = activity?.activity_id || events[0]?.activity_id || '';
  const visibleEvents = projectActivityEvents(events);
  const allRows = activityProcessRows(visibleEvents);
  const isActive = Boolean(activity && !['ending','ended'].includes(activity.status));
  const isEnding = Boolean(activity?.status === 'ending' || ending);
  const isEnded = activity?.status === 'ended' || events.some(event => event.event_type === 'ended');
  const hasQuestion = isActive && allRows.some(row => row.kind === 'question');
  const hasAttention = allRows.some(row => row.process.some(processNeedsAttention)
    || ['device_left','device_failed'].includes(row.event.event_type) && !['left','ok'].includes(row.event.status)
    || row.event.physical_stop === 'unknown');
  const showTimeline = expanded || isActive || isEnding || hasQuestion || hasAttention;
  // Keep the start of the mounted window stable: dropping its first row on
  // every arrival would move the text under the user's eyes even without scrolling.
  const startIndex = windowStart?.activityId === activityId
    ? allRows.findIndex(row => row.key === windowStart.key) : -1;
  const windowIndex = startIndex >= 0 ? startIndex : Math.max(0,allRows.length-16);
  const windowRows = allRows.slice(windowIndex);
  const priorGameEnd = allRows.slice(0,windowIndex).reverse().find(row => row.kind==='game_end');
  if (priorGameEnd) windowRows.unshift(priorGameEnd);
  const rows = expanded || hasQuestion || hasAttention ? allRows : windowRows;
  const latestId = visibleEvents[visibleEvents.length - 1]?.event_id;
  const context = activity?.context || activity?.user_text || activity?.text || events.find(event => event.event_type === 'started')?.text || '';
  const latestSpeech = [...allRows].reverse().find(row => row.kind === 'message');
  const timeLabel = ts => ts ? new Date(Number(ts) < 1e11 ? Number(ts)*1000 : Number(ts))
    .toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}) : '';
  React.useEffect(() => {setExpanded(false);setHasNewEvents(false);},[activityId]);
  React.useEffect(() => {
    if (startIndex < 0 && windowRows.length) setWindowStart({activityId,key:windowRows[0].key});
  },[activityId,startIndex,windowRows[0]?.key]);
  // Activity content shares the chat's scrollport. Observe the latest anchor
  // without moving the reader when messages, disclosures or photos change size.
  React.useEffect(() => {
    if (!showTimeline || !latestRef.current) return;
    const anchor=latestRef.current;
    const scroller=anchor.closest('.chat-panel__scroll');
    const viewport=() => scroller?.getBoundingClientRect() || {top:0,bottom:window.innerHeight};
    const latestVisible=() => {
      const position=anchor.getBoundingClientRect(), bounds=viewport();
      return position.top>=bounds.top && position.bottom<=bounds.bottom+1;
    };
    const arrival=lastArrival.current;
    if (arrival?.activityId===activityId && arrival.latestId!==latestId && !latestVisible()) setHasNewEvents(true);
    lastArrival.current={activityId,latestId};
    const markRead=() => {
      const visible=latestVisible();
      setLatestOutside(!visible);
      if (visible) setHasNewEvents(false);
    };
    const target=scroller || window;
    target.addEventListener('scroll',markRead,{passive:true});
    window.addEventListener('resize',markRead);
    const observer=typeof ResizeObserver==='function' ? new ResizeObserver(markRead) : null;
    observer?.observe(timelineRef.current);
    if (scroller) observer?.observe(scroller);
    markRead();
    return () => {
      target.removeEventListener('scroll',markRead);
      window.removeEventListener('resize',markRead);
      observer?.disconnect();
    };
  },[activityId,latestId,showTimeline]);
  if (!activity && !events.length && !error) return null;
  return <section className={`space-activity${isActive || isEnding ? ' space-activity--live' : ' space-activity--ended'}`} aria-label="空间活动">
    <div className="space-activity__overview">
    <div className="space-activity__heading"><div><small>空间活动</small><h2>{context || '最近的空间活动'}
      <span className="space-activity__live">{isEnding ? '正在结束' : isActive ? '进行中' : isEnded ? '已结束' : '同步中'}</span></h2></div>
      {isActive && <button type="button" className="space-activity__end" onClick={onEnd} disabled={ending}>{ending ? '正在结束…' : '结束活动'}</button>}
    </div>
    {(expanded || isActive || isEnding || hasAttention) && <ActivityMembers activity={activity} events={events} agents={agents}
      unitData={unitData} connected={connected} getName={getName}/>}
    </div>
    {error && <div className="space-activity__error" role="status">{processErrorText(error)}</div>}
    {!showTimeline && !isEnded && <div className="space-activity__summary"><p>{latestSpeech
      ? `${getName(latestSpeech.event.unit_id)}：${processUserText(latestSpeech.event.text) || processErrorText(latestSpeech.event.text)}` : '正在等待设备动态'}</p></div>}
    {showTimeline && <div className="space-activity__timeline" ref={timelineRef}>
      {rows.map((row,index) => {
        const event=row.event, unit=event.agent_id || event.unit_id || 'cloud_orchestrator';
        const gameEnd=activityGameEnd(event);
        const previousSpeech = rows.slice(0,index).reverse().find(item => ['message','question','status','result','game_start','game_end'].includes(item.kind));
        const showName = !previousSpeech || !['message','question'].includes(previousSpeech.kind)
          || (previousSpeech.event.agent_id || previousSpeech.event.unit_id) !== unit
          || previousSpeech.event.target_unit_id !== event.target_unit_id;
        const name=getName(unit), receipt=row.kind==='message' ? activityMessageReceipt(event,events) : '';
        return <div key={row.key} className={`space-activity__item space-activity__item--${row.kind}`}>
          <time className="space-activity__time">{timeLabel(event.ts)}</time><div className="space-activity__content">
            {['game_start','game_end'].includes(row.kind) ? <div className="space-activity__game-end" role="status">
              <strong>{row.kind==='game_start' ? '本局游戏已开始' : '本局游戏已结束'}</strong>
              {gameEnd?.reason && <p>结束原因：{gameEnd.reason}</p>}
            </div> : row.kind==='result' ? <>
              <ActivityGameRound event={event} getName={getName}/>
              {(row.process.length>0 || activityDecisionBasis(event)) && <RunProcess events={[...row.process,...(activityDecisionBasis(event) ? [event] : [])]}
                allEvents={events} agents={agents} getName={getName} finished/>}
            </> : ['message','question'].includes(row.kind) ? <>
              <AgentSpeech compact showName={showName} agent={unit} name={name} target={event.target_unit_id ? getName(event.target_unit_id) : ''}
                text={event.text || ''}>
                {receipt && <small className="space-activity__receipt">{receipt}</small>}
              </AgentSpeech>
              {row.kind==='question' && isActive && <button type="button" className="space-activity__answer" onClick={() => onAnswer?.(event)}>回答{name}</button>}
              {(row.process.length>0 || processHasContent({...event,text:undefined})) && <RunProcess
                events={[...row.process,...(processHasContent({...event,text:undefined}) ? [event] : [])]}
                allEvents={events} agents={agents} getName={getName} finished/>}
            </> : row.kind==='process' ? <>
              {row.process.filter(item => item.game_round).map(item => <ActivityGameRound key={item.event_id} event={item} getName={getName}/>)}
              <RunProcess events={row.process} allEvents={events} agents={agents} getName={getName} finished={isEnded}/>
            </> : <div className="space-activity__status"><p>{activityStatusText(event,name)}</p></div>}
          </div>
        </div>;
      })}
      <div ref={latestRef} className="space-activity__latest" aria-hidden="true"/>
    </div>}
    {(hasNewEvents || latestOutside) && showTimeline && <button type="button" className="space-activity__new" onClick={() => {
      setHasNewEvents(false);
      const anchor=latestRef.current, scroller=anchor?.closest('.chat-panel__scroll');
      if (scroller) scroller.scrollTop=scroller.scrollHeight;
      else anchor?.scrollIntoView({block:'end'});
    }}>{hasNewEvents ? '有新动态 · ' : ''}回到最新 ↓</button>}
    {allRows.length>0 && !hasQuestion && (!isActive || allRows.length>rows.length || expanded) && <button type="button"
      className="space-activity__toggle" aria-label={expanded ? '收起活动详情' : '展开活动详情'} aria-expanded={expanded}
      onClick={() => setExpanded(value => !value)}>
      <span>{expanded ? '收起详情' : '查看详情'}</span><svg className="process-chevron" data-open={expanded} viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="m4 6 4 4 4-4"/></svg>
    </button>}
  </section>;
}
function ActivityGameRound({event,getName}) {
  const round=event.game_round;
  const labels={rock:'石头',paper:'布',scissors:'剪刀'};
  return <div className="activity-game-round" aria-label={`第 ${round.round} 轮揭晓`}><small>第 {round.round} 轮 · 已揭晓</small>
    <div className="activity-game-round__choices">{Object.entries(round.choices || {}).map(([unit,choice]) =>
      <div key={unit}><CompanionAvatar unitId={unit} size={40} mode="history"
        snapshot={{shape:choice}} label={labels[choice]}/>
        <strong>{getName(unit)} · {labels[choice] || choice}</strong><small>{round.mapping?.[unit]?.label || ''}</small></div>)}</div>
    {round.winner && <p>{round.winner==='draw' ? '平局' : `${getName(round.winner)}获胜`}</p>}
  </div>;
}
window.SpaceActivityFeed = SpaceActivityFeed;
