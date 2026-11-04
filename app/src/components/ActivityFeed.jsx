/* ActivityFeed — 主屏活动流组件 */
function ActivityFeed({ entries, agents = [], activity, activityEvents = [], activityHistory = [], activityEnding, activityError,
  onEndActivity, onAnswerActivity, onOpenRules }) {
  const { useRef, useEffect } = React;
  const bottomRef = useRef(null);
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
    if (entries.length) bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [entries]);

  return (
    <div style={{ padding: '0 16px' }}>
      {rows.map(({ entry: e, index: i }) => {
        if (e.type === 'space_activity') {
          const card = e.card;
          return <SpaceActivityFeed key={`activity-${card.id}`}
            activity={card.activity} events={card.events} ending={card.ending}
            error={card.error} onEnd={onEndActivity} onAnswer={onAnswerActivity}
            getName={getName}/>;
        }
        if (e.type === 'process_group') {
          const finished = entries.some(item => item.requestId === e.requestId
            && item.type === 'ai' && item.final);
          const latest = e.items[e.items.length - 1];
          return <details key={`process-${e.requestId}-${i}`} className="run-process-summary">
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
              </div>)}
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
        if (e.type === 'teaching_saved') {
          const teaching = e.teaching || {};
          const card = agents.find(agent => agent.unit_id === teaching.unit_id);
          return <div key={`teaching-${teaching.teaching_id || i}`} className="teaching-saved teaching-card">
            <div className="teaching-card__top">
              <span className="teaching-card__eyebrow">已记住的教导</span>
              <span className="teaching-card__target">{e.unitName}</span>
            </div>
            <h3 className="teaching-card__title">{teaching.name}</h3>
            <div className="teaching-card__sequence">
              <div className="teaching-card__line"><span>当</span><p>{teaching.trigger}</p></div>
              <div className="teaching-card__line"><span>就</span><p>{teaching.behavior}</p></div>
            </div>
            {teaching.binding && <div className="rule-card__mapping">
              <span className="rule-card__mapping-label">实际触发设置</span>
              <strong>{window.ruleObservationCopy(teaching.binding.observation_id, teaching.binding.where)}
                <span aria-hidden="true">→</span>
                {window.ruleActionCopy(teaching.binding.skill_id, teaching.binding.params, card)}</strong>
            </div>}
            <div className="teaching-saved__footer">
              <span>{teaching.execution_status === 'enabled' ? '已启用 · 设备自主模式下生效' : '已记住 · 暂不自动触发'}</span>
              {onOpenRules && <button className="teaching-link" onClick={onOpenRules}>查看教导与规则 <span aria-hidden="true">↗</span></button>}
            </div>
          </div>;
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

function activityActionText(event, name, compact) {
  const action = event.action || '';
  const label = {
    go2_sport: '执行动作', go2_move: '移动', go2_navigate_to: '前往指定地点',
    go2_navigate_to_coordinate: '前往目标位置', explore_frontier: '探索新区域',
    set_led_state: '切换灯光', set_led_color: '调整灯光颜色', blink_led: '闪烁灯光',
  }[action] || (event.unit_id === 'go2' ? '行动' : '调整灯光');
  const subject = name || (event.unit_id === 'go2' ? '机器狗' : '智能灯');
  // A planned action is an intention, never a completion claim.
  if (event.event_type === 'action') return event.text?.trim() || `${compact ? '' : subject}准备${label}`;
  if (event.event_type === 'action_progress' && event.text?.trim()
      && event.text.trim() !== action) return event.text.trim();
  if (event.event_type === 'action_progress') return `${compact ? '' : subject}正在${label}`;
  const outcome = {
    ok: ['explore_frontier', 'go2_navigate_to_coordinate'].includes(action)
      ? '已到达可探索区域' : `已${label}`,
    accepted: `已收到${label}请求`, executing: `正在${label}`,
    blocked: `暂时无法${label}`, rejected: `无法${label}`,
    cancelled: `已停止${label}`, error: `${label}没有成功`,
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
    const kind = activityEventKind(event);
    if ((kind === 'message' || kind === 'observation')
      && (!event.text?.trim() || generic.test(event.text.trim()))) continue;
    if (event.event_type === 'action' && events.some(later =>
      later.unit_id === event.unit_id && later.task_id === event.task_id
      && ['action_progress', 'action_result'].includes(later.event_type))) continue;
    // Replace a running status only with the receipt for the same action step.
    if (event.event_type === 'action_progress' && events.some(later =>
      later.event_type === 'action_result' && later.unit_id === event.unit_id
      && (event.task_id && later.task_id === event.task_id
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
  const completedColorSteps = new Set(events.filter(event =>
    event.unit_id === 'esp32_desk_led' && event.event_type === 'action_result'
    && event.action === 'set_led_color' && event.status === 'ok' && event.task_id
  ).map(event => event.task_id));
  const compact = [];
  let colorRun = [];
  const flush = () => {
    if (!colorRun.length) return;
    const receipts = colorRun.filter(event => event.event_type === 'action_result');
    if (receipts.length > 2) {
      const latestTask = receipts[receipts.length - 1].task_id;
      const older = colorRun.filter(event => event.task_id !== latestTask);
      const lastOlder = older[older.length - 1];
      compact.push({event_id: `color-summary-${lastOlder.event_id}`,
        event_type: 'adjustment_summary', unit_id: 'esp32_desk_led',
        count: receipts.length - 1, ts: lastOlder.ts});
      compact.push(...colorRun.filter(event => event.task_id === latestTask));
    } else {
      compact.push(...colorRun);
    }
    colorRun = [];
  };
  for (const event of events) {
    const isCompletedColorStep = event.unit_id === 'esp32_desk_led'
      && ['action', 'action_result'].includes(event.event_type)
      && event.action === 'set_led_color' && completedColorSteps.has(event.task_id);
    if (isCompletedColorStep) colorRun.push(event);
    else { flush(); compact.push(event); }
  }
  flush();
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
      ? `${compact ? '' : name}已退出${mode}`
      : event.status === 'unknown'
        ? `${compact ? '' : `${name}的`}活动状态还没有确认`
      : `${compact ? '' : name}暂时无法加入${mode}`;
  if (['action', 'action_progress', 'action_result'].includes(type)) return activityActionText(event, name, compact);
  return `${event.unit_id && !compact ? `${name} · ` : ''}${event.text || '有新进展'}`;
}

/* Device messages, observations and confirmed state use different treatments.
 * An invitation therefore never looks like an action that already happened. */
function SpaceActivityFeed({ activity, events, ending, error, onEnd, onAnswer,
  getName = getAgentDisplayName }) {
  const { useEffect, useRef, useState } = React;
  const [expanded, setExpanded] = useState(false);
  const timelineRef = useRef(null);
  const lastTimelineActivity = useRef('');
  const followTimeline = useRef(true);
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
  const showTimeline = expanded || isActive || hasActiveQuestion;
  // Show the current sequence without turning a long autonomous session into
  // an unbounded page. Older events remain available through the toggle.
  const compactEvents = compactActiveActivityEvents(visibleEvents);
  const timelineEvents = expanded || hasActiveQuestion
    ? visibleEvents : compactEvents.slice(-16);
  const latestEventId = timelineEvents[timelineEvents.length - 1]?.event_id;
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
  useEffect(() => setExpanded(false), [activityId, isActive, isEnding]);
  useEffect(() => {
    const timeline = timelineRef.current;
    if (!isActive || !showTimeline || !latestEventId || !timeline) return;
    const newActivity = lastTimelineActivity.current !== activityId;
    if (newActivity || followTimeline.current) timeline.scrollTop = timeline.scrollHeight;
    lastTimelineActivity.current = activityId;
  }, [activityId, isActive, showTimeline, latestEventId]);
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
    {error && <div className="space-activity__error" role="alert">{error}</div>}
    {!showTimeline && <div className="space-activity__summary">
      <span>最新动态</span><p>{summary}</p>
    </div>}
    {showTimeline && <div className="space-activity__timeline" ref={timelineRef}
      onScroll={event => {
        const timeline = event.currentTarget;
        followTimeline.current = timeline.scrollHeight - timeline.scrollTop
          - timeline.clientHeight < 80;
      }}>
      {timelineEvents.map((event, index) => {
        const kind = activityEventKind(event);
        const unitId = event.unit_id || event.agent_id || '';
        const name = unitId ? getName(unitId) : '空间管家';
        const target = event.target_unit_id || event.target || '';
        const previous = timelineEvents[index - 1];
        const sameDevice = previous && (previous.unit_id || previous.agent_id || '') === unitId;
        const showSpeaker = !sameDevice;
        const mediaUrl = activityMediaUrl(event);
        const text = event.text || '';
        return <div key={event.event_id}
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
            </div> : <div className="space-activity__status">
              {activityStatusText(event, name, true)}
            </div>}
          </div>
        </div>;
      })}
    </div>}
    {visibleEvents.length > 0 && !hasActiveQuestion
      && (!isActive || visibleEvents.length > timelineEvents.length || expanded) && <button type="button"
      className="space-activity__toggle" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>
      {expanded ? '收起旧记录' : '查看全部记录'}
    </button>}
  </section>;
}
window.SpaceActivityFeed = SpaceActivityFeed;
