/* ActivityFeed — 主屏活动流组件 */
function ActivityFeed({ entries, agents = [], activity, activityEvents = [], activityHistory = [], activityEnding, activityError,
  onEndActivity, onAnswerActivity }) {
  const { useRef, useEffect } = React;
  const bottomRef = useRef(null);
  const getName = unitId => agents.find(agent => agent.unit_id === unitId)?.name
    || getAgentDisplayName(unitId);
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
    const terminalIndex = entries.findIndex(entry => entry.type === 'process'
      && entry.requestId === requestId && entry.terminal);
    if (terminalIndex >= 0) return terminalIndex;
    const finalIndex = entries.findIndex(entry => entry.type === 'ai'
      && entry.requestId === requestId && entry.final);
    if (finalIndex >= 0) return finalIndex;
    for (let index = entries.length - 1; index >= 0; index--) {
      if (entries[index].requestId === requestId) return index + 1;
    }
    return -1;
  };
  const cardsAt = new Map();
  cards.forEach(card => {
    const anchor = findAnchor(card);
    // A user-triggered activity belongs before that request's final reply.
    // Autonomous activities stay after the chat entries.
    const position = anchor >= 0 ? anchor : card === currentCard ? entries.length : 0;
    cardsAt.set(position, [...(cardsAt.get(position) || []), card]);
  });
  const rows = [];
  for (let index = 0; index <= entries.length; index++) {
    (cardsAt.get(index) || []).forEach(card => rows.push({ entry: { type: 'space_activity', card }, index: -1 }));
    if (index < entries.length) rows.push({ entry: entries[index], index });
  }

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [entries]);

  if (rows.length === 0) return null;
  return (
    <div style={{ padding: '0 16px' }}>
      {rows.map(({ entry: e, index: i }, rowIndex) => {
        if (e.type === 'space_activity') {
          const card = e.card;
          return <SpaceActivityFeed key={`activity-${card.id}`}
            activity={card.activity} events={card.events} ending={card.ending}
            error={card.error} onEnd={onEndActivity} onAnswer={onAnswerActivity}
            getName={getName}/>;
        }
        if (e.type === 'process') {
          const agent = e.agent || 'cloud_orchestrator';
          const previous = rows[rowIndex - 1]?.entry;
          const startsGroup = previous?.type !== 'process' || previous.requestId !== e.requestId
            || previous.agent !== agent;
          const color = getAgentBubbleColor(agent);
          return <div key={e.eventId || `process-${i}`} className={`run-process${e.terminal ? ' run-process--terminal' : ''}`}
            style={{ '--run-color': color }}>
            {startsGroup && <div className="run-process__agent">{getName(agent)}</div>}
            <div className="run-process__detail"><span className="run-process__marker" aria-hidden="true"/>{e.text}</div>
          </div>;
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

function activityMediaUrl(event) {
  const candidate = event.media_ref || event.media_url;
  const path = typeof candidate === 'string' ? candidate : candidate?.url;
  if (typeof path !== 'string' || !path.startsWith('/api/')) return '';
  return path;
}

function activityEventKind(event) {
  const type = String(event.event_type || '').toLowerCase();
  if (type.includes('question') || type.includes('ask_user')) return 'question';
  if (type.includes('observation') || type.includes('observed') || type === 'person_identified') return 'observation';
  if (type.includes('message') || type.includes('proposal') || type.includes('reply')) return 'message';
  return 'status';
}

function activityStatusText(event, name, compact = false) {
  const type = event.event_type;
  const mode = { reactive: '自主调光', free_explore: '自由探索' }[event.mode] || event.mode || '自主模式';
  if (type === 'started') return `活动已开始：${event.text || '设备正在加入'}`;
  if (type === 'ended') return '活动已结束';
  if (type === 'device_joined') return `${compact ? '' : name}已进入${mode}`;
  if (type === 'response') return `${compact ? '' : name}已回应${event.status ? ` · ${event.status}` : ''}`;
  if (type === 'device_left') return event.status === 'offline'
    ? `${compact ? '' : name}离线，未加入活动`
    : event.status === 'left'
      ? `${compact ? '' : name}已退出${mode}${event.text ? ` · ${event.text}` : ''}`
      : event.status === 'unknown'
        ? `${compact ? '' : `${name}的`}模式状态尚未确认${event.text ? ` · ${event.text}` : ''}`
      : `${compact ? '' : name}未加入${mode}${event.text ? ` · ${event.text}` : ''}`;
  if (['action', 'action_progress', 'action_result'].includes(type)) {
    const statusText = {
      executing: '正在执行', accepted: '已收到动作请求，等待确认',
      ok: '动作已完成', blocked: '动作未执行', error: '动作失败',
      timeout: '动作结果尚未确认', unknown: '动作结果尚未确认',
      cancelled: '动作已取消', rejected: '已拒绝动作',
    }[event.status] || '动作状态已更新';
    return compact ? statusText : `${name} · ${statusText}`;
  }
  return `${event.unit_id && !compact ? `${name} · ` : ''}${event.text || '状态已更新'}`;
}

/* Device messages, observations and confirmed state use different treatments.
 * An invitation therefore never looks like an action that already happened. */
function SpaceActivityFeed({ activity, events, ending, error, onEnd, onAnswer,
  getName = getAgentDisplayName }) {
  const { useEffect, useRef, useState } = React;
  const [expanded, setExpanded] = useState(false);
  const latestMessageRef = useRef(null);
  let latestMessageIndex = -1;
  events.forEach((event, index) => {
    if (activityEventKind(event) === 'message') latestMessageIndex = index;
  });
  const latestMessageId = events[latestMessageIndex]?.event_id;
  const context = activity?.context || activity?.user_text || activity?.text ||
    events.find(event => event.event_type === 'started')?.text || '';
  const activityId = activity?.activity_id || events[0]?.activity_id || '';
  const isActive = Boolean(activity && !['ending', 'ended'].includes(activity.status));
  const isEnding = Boolean(activity?.status === 'ending' || ending);
  const isEnded = activity?.status === 'ended' || events.some(event => event.event_type === 'ended');
  const isPending = !activity && !isEnded && events.length > 0;
  const showTimeline = isActive || isEnding || isPending || expanded;
  const summaryEvent = [...events].reverse().find(event =>
    ['device_left', 'action_result'].includes(event.event_type));
  const summary = summaryEvent
    ? activityStatusText(summaryEvent, summaryEvent.unit_id
      ? getName(summaryEvent.unit_id) : '空间管家')
    : '本次活动已结束';
  useEffect(() => setExpanded(false), [activityId, isActive, isEnding]);
  useEffect(() => {
    if (isActive && showTimeline && latestMessageId) {
      latestMessageRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }, [activityId, isActive, showTimeline, latestMessageId]);
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
      {isEnded && events.length > 0 && <button type="button"
        className="space-activity__toggle" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>
        {expanded ? '收起记录' : '查看记录'}
      </button>}
    </div>
    {error && <div className="space-activity__error" role="alert">{error}</div>}
    {!showTimeline && events.length > 0 && <div className="space-activity__summary">{summary}</div>}
    {showTimeline && <div className="space-activity__timeline">
      {events.map((event, index) => {
        const kind = activityEventKind(event);
        const unitId = event.unit_id || event.agent_id || '';
        const name = unitId ? getName(unitId) : '空间管家';
        const target = event.target_unit_id || event.target || '';
        const previous = events[index - 1];
        const sameDevice = previous && (previous.unit_id || previous.agent_id || '') === unitId;
        const showSpeaker = !sameDevice || (kind === 'message' || kind === 'question')
          && (previous.target_unit_id || previous.target || '') !== target;
        const mediaUrl = activityMediaUrl(event);
        const text = event.text || '';
        return <div key={event.event_id}
          ref={index === latestMessageIndex ? latestMessageRef : null}
          className={`space-activity__item space-activity__item--${kind}`}>
          <span className="space-activity__time">{timeLabel(event.ts)}</span>
          <div className="space-activity__content">
            {showSpeaker && <div className="space-activity__speaker">{name}{target ? ` → ${getName(target)}` : ''}
              {event.reply_to ? ' · 回应' : ''}</div>}
            {kind === 'message' || kind === 'question' ? <>
              <div className="space-activity__bubble">{text || (kind === 'question' ? '需要你帮忙看一下。' : '发送了一条消息')}
                {mediaUrl && <a href={mediaUrl} target="_blank" rel="noopener noreferrer">查看截图</a>}
              </div>
              {kind === 'question' && isActive && <button type="button" className="space-activity__answer" onClick={() => onAnswer?.(event)}>
                回答{name}
              </button>}
            </> : kind === 'observation' ? <div className="space-activity__observation">
              <span>看到了</span><p>{text || '一条新观察'}</p>
              {mediaUrl && <a href={mediaUrl} target="_blank" rel="noopener noreferrer">查看截图</a>}
            </div> : <div className="space-activity__status">{activityStatusText(event, name, true)}</div>}
          </div>
        </div>;
      })}
    </div>}
  </section>;
}
window.SpaceActivityFeed = SpaceActivityFeed;
