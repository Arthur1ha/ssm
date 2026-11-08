/* ActivityFeed — 主屏活动流组件 */
function ActivityFeed({ entries, agents = [], unitData = {}, connected = false, activity, activityEvents = [], activityHistory = [], activityEnding, activityError,
  onEndActivity, onAnswerActivity, onOpenRules }) {
  const { useRef, useEffect } = React;
  const bottomRef = useRef(null);
  const currentActivityRef = useRef(null);
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
    const requestId = owner?.requestId || (!sourceEventId && card !== currentCard && context
      ? [...entries].reverse().find(entry => entry.type === 'user' && entry.text === context)?.requestId
      : null);
    if (!requestId) return -1;
    const finalIndex = entries.findIndex(entry => entry.type === 'ai'
      && entry.requestId === requestId && entry.final);
    if (finalIndex >= 0) return finalIndex + 1;
    const terminalIndex = entries.findIndex(entry => entry.type === 'process'
      && entry.requestId === requestId && entry.terminal);
    if (terminalIndex >= 0) return terminalIndex + 1;
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
  const rows = [];
  for (let index = 0; index <= entries.length; index++) {
    (cardsAt.get(index) || []).forEach(card => rows.push({ entry: { type: 'space_activity', card }, index: -1 }));
    if (index >= entries.length) continue;
    const entry = entries[index];
    const previous = rows[rows.length - 1]?.entry;
    if (entry.type === 'process' && previous?.type === 'process_group'
      && previous.requestId === entry.requestId) {
      previous.items.push(entry);
    } else if (entry.type === 'process') {
      rows.push({ entry: { type: 'process_group', requestId: entry.requestId,
        items: [entry] }, index });
    } else {
      rows.push({ entry, index });
    }
  }
  rows.unshift({ entry: { type: 'ai', agent: 'cloud_orchestrator',
    text: getAgentOpeningLine('cloud_orchestrator'), eventId: 'welcome' }, index: 'welcome' });

  useEffect(() => {
    if (!entries.length) return;
    if (activity?.activity_id) {
      currentActivityRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } else {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [entries, activity?.activity_id]);
  useEffect(() => {
    if (activity?.activity_id) {
      currentActivityRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }, [activity?.activity_id]);

  return (
    <div style={{ padding: '0 16px' }}>
      {rows.map(({ entry: e, index: i }) => {
        if (e.type === 'space_activity') {
          const card = e.card;
          return <div key={`activity-${card.id}`} ref={card === currentCard ? currentActivityRef : null}>
            <SpaceActivityFeed
              activity={card.activity} events={card.events} ending={card.ending}
              error={card.error} onEnd={onEndActivity} onAnswer={onAnswerActivity}
              getName={getName} agents={agents} unitData={unitData} connected={connected}/>
          </div>;
        }
        if (e.type === 'process_group') {
          const finished = entries.some(item => item.requestId === e.requestId
            && item.type === 'ai' && item.final);
          const latest = e.items[e.items.length - 1];
          return <details key={`process-${e.requestId}-${i}`} className="run-process-summary"
            open={!finished && latest.stage?.startsWith('search_') ? true : undefined}>
            <summary>
              <span className="run-process-summary__marker" aria-hidden="true"/>
              <span className="run-process-summary__text">{finished
                ? `处理过程 · ${e.items.length} 条记录` : `正在处理 · ${latest.text}`}</span>
              <span className="run-process-summary__toggle" aria-hidden="true"/>
            </summary>
            <div className="run-process-summary__list">
              {e.items.map((item, itemIndex) => <div key={item.eventId || itemIndex}
                className="run-process-summary__item">
                <span>{getName(item.agent || 'cloud_orchestrator')}</span>
                <p>{item.text}</p>
                {activityMediaUrl(item) && <img src={activityMediaUrl(item)} alt="搜索观察照片"
                  loading="lazy" style={{ maxWidth: '100%', maxHeight: 220, borderRadius: 8 }}/>}
              </div>)}
              {!finished && latest.taskId && latest.stage?.startsWith('search_')
                && latest.stage !== 'search_finished' && <SearchTaskStop taskId={latest.taskId}/>}
            </div>
          </details>;
        }
        if (e.type === 'user') {
          return (
            <div key={i} style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 8 }}>
              <div style={{ maxWidth: '78%', display: 'flex', flexDirection: 'column',
                alignItems: 'flex-end', gap: 4 }}>
                <div style={{ fontSize: 10, paddingRight: 4, color: 'var(--color-accent)',
                  opacity: 0.75, fontFamily: 'var(--font-mono)', letterSpacing: '0.04em' }}>你</div>
                <div style={{
                  padding: '10px 14px', fontSize: 14, lineHeight: 1.5,
                  borderRadius: 'var(--radius-card)',
                  background: 'var(--color-accent)', color: 'var(--color-bg)',
                }}>{e.text}</div>
              </div>
            </div>
          );
        }
        if (e.type === 'ai' || e.type === 'peer_message') {
          const agent = e.sender || e.agent || 'orchestrator';
          const oc = getAgentBubbleColor(agent);
          return (
            <div key={e.eventId || `${e.type}-${i}`} style={{ display: 'flex', justifyContent: 'flex-start',
              marginBottom: 8 }}>
              <div style={{ maxWidth: '88%', minWidth: 0, display: 'flex',
                flexDirection: 'column', gap: 4 }}>
                <div style={{ fontSize: 10, paddingLeft: 4, color: oc, opacity: 0.75,
                  fontFamily: 'var(--font-mono)', letterSpacing: '0.04em' }}>
                  {getName(agent)}{e.type === 'peer_message' && e.target
                    ? ` → ${getName(e.target)}${e.replyTo ? ' · 回应' : ''}` : ''}
                </div>
                <div style={{
                  padding: '10px 14px', fontSize: 14, lineHeight: 1.6,
                  borderRadius: 'var(--radius-card)',
                  background: `${oc}10`,
                  border: `1px solid ${oc}25`,
                  color: 'var(--color-text)',
                }}>{e.text}</div>
              </div>
            </div>
          );
        }
        if (e.type === 'event') {
          /* 设备状态变化：居中的低存在感小药丸，明显区别于左右对话气泡，不抢戏 */
          return (
            <div key={i} style={{ display: 'flex', justifyContent: 'center', marginBottom: 8 }}>
              <span style={{
                fontSize: 10.5, color: 'var(--color-text-dim)',
                fontFamily: 'var(--font-mono)', letterSpacing: '0.02em',
                background: 'var(--color-surface-2)', border: '1px solid var(--color-border)',
                borderRadius: 999, padding: '3px 10px', opacity: 0.85,
              }}>
                {e.text}
                <span style={{ marginLeft: 6, opacity: 0.6 }}>
                  {new Date(e.ts).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}
                </span>
              </span>
            </div>
          );
        }
        if (e.type === 'system') {
          return (
            <div key={i} style={{ marginBottom: 8, display: 'flex', alignItems: 'center', gap: 8 }}>
              <div style={{ flex: 1, height: 1, background: 'var(--color-border)' }}/>
              <span style={{
                fontSize: 11, color: 'var(--color-text-dim)',
                fontFamily: 'var(--font-mono)', whiteSpace: 'nowrap',
              }}>{e.text}</span>
              <div style={{ flex: 1, height: 1, background: 'var(--color-border)' }}/>
            </div>
          );
        }
        return null;
      })}

      <div ref={bottomRef}/>
    </div>
  );
}

window.ActivityFeed = ActivityFeed;

function SearchTaskStop({ taskId }) {
  const [pending, setPending] = React.useState(false);
  const [message, setMessage] = React.useState('');
  const stop = async () => {
    if (pending) return;
    setPending(true);
    try {
      const result = await apiClient.request(`/api/commands/go2/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' });
      setMessage(result.result === 'cancelled' ? '搜索已取消，设备已确认停止'
        : '已请求停止，尚未确认设备停稳');
    } catch (error) {
      setMessage(apiErrorText(error, '停止请求未确认，请使用设备急停'));
    } finally {
      setPending(false);
    }
  };
  return <div><button type="button" disabled={pending} onClick={stop}>
    {pending ? '正在请求停止…' : '停止搜索'}</button>
    {message && <p role="status">{message}</p>}</div>;
}

function activityMediaUrl(event) {
  const candidate = event.media_ref || event.media_url;
  const path = typeof candidate === 'string' ? candidate : candidate?.url;
  if (typeof path !== 'string' || !path.startsWith('/api/')) return '';
  return path;
}

function activityEventKind(event = {}) {
  const type = String(event.event_type || '').toLowerCase();
  if (type.includes('question') || type.includes('ask_user')) return 'question';
  if (type.includes('observation') || type.includes('observed') || type === 'person_identified') return 'observation';
  if (type.includes('message') || type.includes('proposal') || type.includes('reply')) return 'message';
  return 'status';
}

function activityActionText(event, name, compact) {
  const action = event.action || '';
  const label = {
    go2_sport: '执行动作', go2_move: '移动', go2_navigate_to: '前往指定地点',
    go2_observe: '观察周围', go2_navigate_to_visible_target: '安全接近目标',
    go2_navigate_to_coordinate: '前往目标位置', explore_frontier: '探索新区域',
    set_led_state: '切换灯光', set_led_color: '调整灯光颜色', blink_led: '闪烁灯光',
  }[action] || (event.unit_id === 'go2' ? '行动' : '调整灯光');
  const subject = name || (event.unit_id === 'go2' ? '机器狗' : '智能灯');
  if (event.event_type === 'action_result' && event.status === 'ok' && isActivityLightingAction(action)) {
    const state = event.state_at_event?.status === 'current' ? event.state_at_event.values : null;
    if (state?.ism === 'OFF') return '灯已熄灭';
    if (state?.ism) return `已调为${activityPhysicalLabel({tags:['lighting']}, state)}`;
    if (event.lighting_summary) return '灯光已调整';
  }
  // A planned action is an intention, never a completion claim.
  if (event.event_type === 'action') return event.text?.trim() || `${compact ? '' : subject}准备${label}`;
  if (event.event_type === 'action_progress' && event.text?.trim()
      && event.text.trim() !== action) return event.text.trim();
  if (event.event_type === 'action_progress') return `${compact ? '' : subject}正在${label}`;
  const outcome = {
    ok: ['explore_frontier', 'go2_navigate_to_coordinate'].includes(action)
      ? '自由探索已到达新的地图探索点' : `已${label}`,
    accepted: `已收到${label}请求`, executing: `正在${label}`,
    blocked: `暂时无法${label}`, rejected: `无法${label}`,
    cancelled: `本次${label}已取消`, error: `${label}没有成功`,
    timeout: `${label}的结果还没有确认`, unknown: `${label}的结果还没有确认`,
  }[event.status] || `${label}的状态已更新`;
  return `${compact ? '' : subject}${outcome}`;
}

function projectActivityEvents(events) {
  const generic = /^(?:ok|error|blocked|accepted|executing|unknown|动作已完成|处理结束|(?:空间管家)?(?:已完成[，,、·\s]*)+)$/i;
  const visible = [];
  for (const event of events) {
    if (event.display === false) continue;
    if (event.event_type === 'started') continue; // The activity heading already says this.
    if (event.event_type === 'response' && event.status === 'ok') continue;
    // Message delivery is already expressed under the conversation. It is not
    // another device action and does not deserve its own progress row.
    if (event.event_type === 'action_progress' && !event.action && event.status === 'accepted') continue;
    // A peer's final receipt repeats a typed device result for the same task.
    // Keep it in raw history for evidence, not as another user-visible action.
    if (event.event_type === 'action_result' && !event.action && event.task_id && events.some(item =>
      item.event_type === 'action_result' && item.unit_id === event.unit_id
      && item.task_id === event.task_id && item.action && item.status === event.status
      && item.display !== false)) continue;
    const kind = activityEventKind(event);
    if ((kind === 'message' || kind === 'observation')
      && (!event.text?.trim() || generic.test(event.text.trim()))) continue;
    if (event.event_type === 'action' && event.task_id && events.some(later =>
      later.unit_id === event.unit_id && later.task_id === event.task_id && later.action === event.action
      && later.ts >= event.ts
      && ['action_progress', 'action_result'].includes(later.event_type))) continue;
    // Replace a running status only with the receipt for the same action step.
    if (event.event_type === 'action_progress' && events.some(later =>
      later.event_type === 'action_result' && later.unit_id === event.unit_id
      && later.ts >= event.ts
      && (event.task_id && later.task_id === event.task_id && later.action === event.action
        || !event.task_id && later.action === event.action
          && later.ts >= event.ts && later.ts - event.ts < 30))) continue;
    // Keep confirmed actions alongside the device's later persona reply.
    const text = (kind === 'message' || kind === 'question' || kind === 'observation')
      ? (event.text || '').trim() : activityStatusText(event, '', true);
    const prior = [...visible].reverse().find(item => item.unit_id === event.unit_id
      && Math.abs((item.ts || 0) - (event.ts || 0)) < 30);
    if (prior && (!event.task_id || prior.task_id === event.task_id)
      && (prior.event_type === event.event_type
      || (['message', 'observation'].includes(activityEventKind(prior))
        && ['message', 'observation'].includes(kind)))) {
      const priorKind = activityEventKind(prior);
      const priorText = ['message', 'question', 'observation'].includes(priorKind)
        ? (prior.text || '').trim() : activityStatusText(prior, '', true);
      if (text && text === priorText) continue;
    }
    visible.push(event);
  }
  return visible;
}

function compactActiveActivityEvents(events) {
  const compact = [];
  for (const event of events) {
    const previous = compact[compact.length - 1];
    const lighting = item => item?.event_type === 'action_result' && item.status === 'ok'
      && isActivityLightingAction(item.action);
    // Power-on and color-setting are steps of one explicitly linked request.
    // Present its final light once; retain both physical tasks in the inspector.
    if (lighting(event) && lighting(previous) && event.unit_id === previous.unit_id
      && event.trigger_message_id && event.trigger_message_id === previous.trigger_message_id) {
      compact[compact.length - 1] = {...event, lighting_summary:true,
        related_task_ids:[...new Set([...(previous.related_task_ids || [previous.task_id]), event.task_id].filter(Boolean))]};
    } else compact.push(event);
  }
  return compact;
}

function activityStatusText(event, name, compact = false) {
  const type = event.event_type;
  const mode = { reactive: '自主调光', free_explore: '自由探索' }[event.mode] || event.mode || '自主模式';
  if (type === 'adjustment_summary') return `${name}连续调整了灯光 ${event.count} 次`;
  if (type === 'started') return '活动已开始';
  if (type === 'ended') return '活动已结束';
  if (type === 'device_joined') return `${compact ? '' : name}已进入${mode}`;
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
  if (['action', 'action_progress', 'action_result'].includes(type)) return activityActionText(event, name, compact);
  return `${event.unit_id && !compact ? `${name} · ` : ''}${event.text || '有新进展'}`;
}

/* Device messages, observations and confirmed state use different treatments.
 * An invitation therefore never looks like an action that already happened. */
function SpaceActivityFeed({ activity, events, ending, error, onEnd, onAnswer,
  agents = [], unitData = {}, connected = false,
  getName = getAgentDisplayName }) {
  const { useEffect, useRef, useState } = React;
  const [expanded, setExpanded] = useState(false);
  const [selectedEvent, setSelectedEvent] = useState(null);
  const [hasNewEvents, setHasNewEvents] = useState(false);
  const [readingWindow, setReadingWindow] = useState(null);
  const timelineRef = useRef(null);
  const lastTimelineActivity = useRef('');
  const followTimeline = useRef(true);
  const lastSeenEvent = useRef(null);
  const visibleEvents = projectActivityEvents(events);
  const context = activity?.context || activity?.user_text || activity?.text ||
    events.find(event => event.event_type === 'started')?.text || '';
  const activityId = activity?.activity_id || events[0]?.activity_id || '';
  const isActive = Boolean(activity && !['ending', 'ended'].includes(activity.status));
  const isEnding = Boolean(activity?.status === 'ending' || ending);
  const isEnded = activity?.status === 'ended' || events.some(event => event.event_type === 'ended');
  const isPending = !activity && !isEnded && events.length > 0;
  // Keep questions visible so the user can answer without opening the record.
  const hasActiveQuestion = isActive && visibleEvents.some(event =>
    activityEventKind(event) === 'question');
  const showTimeline = expanded || isActive || isEnding || hasActiveQuestion;
  // Show the current sequence without turning a long autonomous session into
  // an unbounded page. Older events remain available through the toggle.
  const compactEvents = compactActiveActivityEvents(activityTaskRows(visibleEvents));
  const incomingTimeline = expanded || hasActiveQuestion
    ? compactEvents : compactEvents.slice(-16);
  const timelineEvents = readingWindow || incomingTimeline;
  const latestEventId = incomingTimeline[incomingTimeline.length - 1]?.event_id;
  const summaryEvent = [...visibleEvents].reverse().find(event => event.event_type !== 'ended');
  const summaryName = summaryEvent?.unit_id || summaryEvent?.agent_id
    ? getName(summaryEvent.unit_id || summaryEvent.agent_id) : '空间管家';
  const summary = summaryEvent
    ? ['message', 'observation', 'question'].includes(activityEventKind(summaryEvent))
      ? `${summaryName}：${(summaryEvent.text || '').slice(0, 140)}`
      : summaryEvent.event_type === 'action' && summaryEvent.text
        ? `${summaryName}：${summaryEvent.text}`
      : activityStatusText(summaryEvent, summaryName)
    : isEnded ? '本次活动已结束' : '正在等待设备动态';
  useEffect(() => {
    setExpanded(false); setSelectedEvent(null); setHasNewEvents(false); setReadingWindow(null);
    followTimeline.current = true;
    lastSeenEvent.current = null;
  }, [activityId]);
  useEffect(() => {
    const timeline = timelineRef.current;
    if (!showTimeline || !latestEventId || !timeline) return;
    const newActivity = lastTimelineActivity.current !== activityId;
    if (newActivity || followTimeline.current) {
      timeline.scrollTop = timeline.scrollHeight;
      setHasNewEvents(false);
      lastSeenEvent.current = latestEventId;
    } else setHasNewEvents(lastSeenEvent.current !== latestEventId);
    lastTimelineActivity.current = activityId;
  }, [activityId, isActive, showTimeline, latestEventId, readingWindow]);
  if (!activity && events.length === 0 && !error) return null;
  const timeLabel = ts => ts ? new Date(Number(ts) < 1e11 ? Number(ts) * 1000 : Number(ts))
    .toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '';

  return <section className={`space-activity${isActive || isEnding || isPending ? ' space-activity--live' : ' space-activity--ended'}`} aria-label="空间活动">
    <div className="space-activity__heading">
      <div>
        <small>空间活动</small>
        <h2>{context || '最近的空间活动'}{(activity || events.length > 0) &&
          <span className="space-activity__live">
            {isEnding ? '正在结束' : isActive ? '进行中' : isPending ? '同步中' : '已结束'}</span>}</h2>
      </div>
      {isActive && <button type="button" className="space-activity__end" onClick={onEnd} disabled={ending}>
        {ending ? '正在结束…' : '结束活动'}
      </button>}
    </div>
    {(expanded || isActive || isEnding) && <ActivityMembers activity={activity} events={events}
      agents={agents} unitData={unitData} connected={connected} getName={getName}/>}
    {error && <div className="space-activity__error" role="alert">{error}</div>}
    {!showTimeline && <div className="space-activity__summary">
      <span>最新动态</span><p>{summary}</p>
    </div>}
    {showTimeline && <div className="space-activity__timeline" ref={timelineRef}
      onScroll={event => {
        const timeline = event.currentTarget;
        const nearBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 80;
        if (!nearBottom && followTimeline.current) setReadingWindow(timelineEvents);
        followTimeline.current = nearBottom;
        if (nearBottom) {
          setHasNewEvents(false); setReadingWindow(null); lastSeenEvent.current = latestEventId;
        }
      }}>
      {timelineEvents.map((event, index) => {
        const kind = activityEventKind(event);
        const unitId = event.unit_id || event.agent_id || '';
        const name = unitId ? getName(unitId) : '空间管家';
        const target = event.target_unit_id || event.target || '';
        const previous = timelineEvents[index - 1];
        const sameDevice = previous && (previous.unit_id || previous.agent_id || '') === unitId;
        const showSpeaker = !sameDevice || activityEventKind(previous) !== kind
          || (previous?.target_unit_id || previous?.target || '') !== target;
        const mediaUrl = activityMediaUrl(event);
        const text = event.text || '';
        const receipt = kind === 'message' ? activityMessageReceipt(event, events) : '';
        const linkedTask = kind === 'message' && activityRelatedEvents(event, events).task.length > 0;
        const actionEvent = ['action', 'action_progress', 'action_result'].includes(event.event_type);
        const change = actionEvent ? activityFeedbackChange(event, events,
          agents.find(agent => agent.unit_id === unitId)) : '';
        return <div key={event.event_id}
          className={`space-activity__item space-activity__item--${kind}`}
          style={{'--activity-speaker':getAgentBubbleColor(unitId)}}>
          <span className="space-activity__time">{timeLabel(event.ts)}</span>
          <div className="space-activity__content">
            {showSpeaker && <div className="space-activity__speaker">{name}{target ? ` → ${getName(target)}` : ''}
              {event.reply_to ? ' · 回应' : ''}</div>}
            {kind === 'message' || kind === 'question' ? <>
              <div className="space-activity__bubble">{text || (kind === 'question' ? '需要你帮忙看一下。' : '发送了一条消息')}
                {mediaUrl && <a href={mediaUrl} target="_blank" rel="noopener noreferrer">查看截图</a>}
              </div>
              {(receipt || linkedTask) && <div className="space-activity__message-meta">
                {receipt && <span className="space-activity__receipt">{receipt}</span>}
                {linkedTask && <button type="button" className="space-activity__evidence-link"
                  onClick={() => setSelectedEvent(event)}>查看进展<Icon name="arrow" size={12}/></button>}
              </div>}
              {kind === 'question' && isActive && <button type="button" className="space-activity__answer" onClick={() => onAnswer?.(event)}>
                回答{name}
              </button>}
            </> : kind === 'observation' ? <div className="space-activity__observation">
              <span>观察</span><p>{text || '一条新观察'}</p>
              {mediaUrl && <a href={mediaUrl} target="_blank" rel="noopener noreferrer">查看截图</a>}
            </div> : <div className={actionEvent ? 'space-activity__action' : 'space-activity__status'}
              data-outcome={actionEvent ? event.status || 'executing' : undefined}>
              <p>{activityStatusText(event, name, true)}</p>
              {change && <span className="space-activity__feedback-change">{change}</span>}
              {actionEvent && <button type="button" className="space-activity__evidence-link"
                onClick={() => setSelectedEvent(event)}>查看变化依据<Icon name="arrow" size={12}/></button>}
            </div>}
          </div>
        </div>;
      })}
    </div>}
    {hasNewEvents && showTimeline && <button type="button" className="space-activity__new"
      onClick={() => {
        followTimeline.current = true; setHasNewEvents(false); setReadingWindow(null);
        if (timelineRef.current) timelineRef.current.scrollTop = timelineRef.current.scrollHeight;
      }}>有新动态 · 回到最新 ↓</button>}
    {visibleEvents.length > 0 && !hasActiveQuestion
      && (!isActive || compactEvents.length > timelineEvents.length || expanded) && <button type="button"
      className="space-activity__toggle" aria-expanded={expanded} onClick={() => {
        setReadingWindow(null); setExpanded(value => !value);
      }}>
      {expanded ? '收起旧记录' : '查看全部记录'}
    </button>}
    {selectedEvent && <ActivityEvidenceDrawer event={selectedEvent} events={events} agents={agents}
      getName={getName} onClose={() => setSelectedEvent(null)}/>}
  </section>;
}
window.SpaceActivityFeed = SpaceActivityFeed;
