/* One disclosure for chat and activity, backed only by public execution facts. */
function ObservationPhoto({event}) {
  const url = activityMediaUrl(event);
  const ts = Number(event.ts || event.media_ref?.ts);
  const time = Number.isFinite(ts) && ts > 0 ? new Date(ts < 1e11 ? ts * 1000 : ts)
    .toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '';
  const caption = String(event.requested_text || event.text || '设备观察照片').slice(0,48);
  return url ? <a className="process-photo" href={url} target="_blank" rel="noopener noreferrer"
    aria-label="打开观察照片"><img src={url} alt={caption} loading="lazy"/>
    <span>{caption}</span><small>{time ? `观察记录 · ${time}` : '照片记录'} ↗</small></a> : null;
}
function AgentSpeech({agent, name, target, text, compact = false, children}) {
  return <div className={`agent-speech${compact ? ' agent-speech--compact' : ''}`}
    style={{'--speaker-color':getAgentBubbleColor(agent)}}>
    <div className="agent-speech__name">{name}{target ? ` → ${target}` : ''}</div>
    <div className="agent-speech__bubble">{text}</div>{children}
  </div>;
}
function RunProcess({events = [], allEvents = events, agents = [], getName = getAgentDisplayName, finished = false, contextText = ''}) {
  const [open,setOpen] = React.useState(false);
  const steps = processSteps(events);
  if (!steps.length) return null;
  const attention = steps.filter(step => processNeedsAttention(step.event));
  const capabilitySteps = steps.filter(step => step.event.tool || step.event.action);
  const last = steps[steps.length - 1].event;
  const lastCapability = capabilitySteps[capabilitySteps.length - 1]?.event;
  const detail = lastCapability ? processArguments(lastCapability,agents).map(arg => arg.value).join(' · ') : '';
  const description = lastCapability ? [processCapability(lastCapability,agents),detail,processOutcome(lastCapability)].filter(Boolean).join(' · ')
    : activityMediaUrl(last) ? '观察周围' : finished ? '处理结束' : last.text || '整理上下文';
  const contextIds = new Set(events.map(event => event.trigger_message_id).filter(Boolean));
  const contexts = allEvents.filter(event => event.message_id && contextIds.has(event.message_id)
    && ['message','question'].includes(activityEventKind(event)));
  return <div className="run-process" data-process>
    {attention.length > 0 && <p className="run-process__attention" role="status">
      {attention.map(step => [processCapability(step.event,agents),processOutcome(step.event),activityReasonText(step.event)].filter(Boolean).join(' · ')).join('；')}
    </p>}
    <details className="run-process-summary" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
      <summary aria-label="处理过程" aria-expanded={open}>
        <span className="run-process-summary__marker" aria-hidden="true"/>
        <span className="run-process-summary__text"><strong>处理过程</strong><span>{description}</span></span>
        <svg className="process-chevron" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="m4 6 4 4 4-4"/></svg>
      </summary>
      <div className="run-process__body">
        {(contextText || contexts.length > 0) && <section className="run-process__context"><h3>上下文</h3>
          {contextText && <blockquote><small>你</small><p>{contextText}</p></blockquote>}
          {contexts.map(event => <blockquote key={event.event_id || event.message_id}><small>{getName(event.unit_id)}</small><p>{event.text}</p></blockquote>)}
        </section>}
        <ol className="run-process__steps">{steps.map(({key,event}) => {
          const tool = event.tool || event.action;
          const name = getName(event.target_unit_id || event.unit_id || event.agent || 'cloud_orchestrator');
          const basis = activityDecisionBasis(event);
          const args = processArguments(event,agents);
          const reason = activityReasonText(event);
          const snapshot = event.state_at_event;
          const card = agents.find(agent => agent.unit_id === (event.unit_id || event.agent));
          const facts = snapshot?.status === 'current' ? activityPhysicalLabel(card,snapshot.values) : '';
          const text = event.text || '';
          return <li className="run-process__step" key={key} data-step-id={event.step_id || undefined}>
            <span className="run-process__step-dot" aria-hidden="true"/>
            <div className="run-process__step-content"><header><strong>{tool ? processCapability(event,agents)
              : activityMediaUrl(event) || activityEventKind(event) === 'observation' ? '观察' : processOutcome(event) || '处理'}</strong>
              {tool && <span data-outcome={event.status}>{processOutcome(event)}</span>}</header>
              <small className="run-process__actor">{name}</small>
              {event.requested_text && event.requested_text !== text && <p className="run-process__note">{event.requested_text}</p>}
              {args.length > 0 && <dl className="run-process__arguments">{args.map(arg => <div key={arg.label}><dt>{arg.label}</dt><dd>
                {arg.color && <i style={{background:arg.color}} aria-hidden="true"/>}{arg.value}</dd></div>)}</dl>}
              {basis && <div className="run-process__decision" data-decision-reason><small>选择说明</small><p>{basis.reason}</p>
                {basis.evidence.map((item,i) => {const evidence = activityDecisionEvidence(item,event,allEvents);return <blockquote key={`${item.ref}-${i}`} data-decision-evidence>
                  <small>{evidence.label}{evidence.source ? ` · ${getName(evidence.source)}` : ''}</small><p>{evidence.summary}</p>
                  <small>{evidence.notice}{evidence.missingOriginal ? '；原记录未保留' : ''}</small>
                </blockquote>;})}</div>}
              {text && !activityMediaUrl(event) && (!tool || (!args.length && !basis) || processNeedsAttention(event)) && <p className="run-process__note">{text}</p>}
              {reason && reason !== text && <p className="run-process__note">{reason}</p>}
              {facts && !facts.includes('待确认') && <p className="run-process__feedback">当时设备反馈 · {facts}</p>}
              <ObservationPhoto event={event}/>
            </div>
          </li>;
        })}</ol>
      </div>
    </details>
  </div>;
}
Object.assign(window,{RunProcess,ObservationPhoto,AgentSpeech});
