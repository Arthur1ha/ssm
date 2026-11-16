/* One disclosure for chat and activity, backed only by public execution facts. */
function ObservationPhoto({event}) {
  const url = activityMediaUrl(event);
  const ts = Number(event.ts || event.media_ref?.ts);
  const time = Number.isFinite(ts) && ts > 0 ? new Date(ts < 1e11 ? ts * 1000 : ts)
    .toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '';
  const caption = String(event.requested_text || event.text || '设备观察照片').slice(0,48);
  return url ? <a className="process-photo" href={url} target="_blank" rel="noopener noreferrer"
    aria-label="打开观察照片"><img src={url} alt={caption} loading="lazy"/>
    <small>{time ? `查看观察照片 · ${time}` : '查看观察照片'} ↗</small></a> : null;
}
function AgentSpeech({agent, name, target, text, compact = false, children}) {
  return <div className={`agent-speech${compact ? ' agent-speech--compact' : ''}`}
    style={{'--speaker-color':getAgentBubbleColor(agent)}}>
    <div className="agent-speech__name">{name}{target ? ` → ${target}` : ''}</div>
    <div className="agent-speech__bubble">{text}</div>{children}
  </div>;
}
function RunProcess({events = [], allEvents = events, agents = [], getName = getAgentDisplayName, finished = false}) {
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
        <ol className="run-process__steps">{steps.map(({key,event}) => {
          const tool = event.tool || event.action;
          const actor = event.unit_id || event.agent || event.agent_id || 'cloud_orchestrator';
          const name = getName(actor);
          const target = event.target_unit_id && event.target_unit_id !== actor ? getName(event.target_unit_id) : '';
          const basis = activityDecisionBasis(event);
          const args = processArguments(event,agents);
          const reason = activityReasonText(event);
          const snapshot = event.state_at_event;
          const card = agents.find(agent => agent.unit_id === (event.unit_id || event.agent));
          const facts = snapshot?.status === 'current' ? activityPhysicalLabel(card,snapshot.values) : '';
          const text = (event.text || '').trim();
          const speech = ['message','question'].includes(activityEventKind(event));
          const outcome = processOutcome(event);
          const title = tool ? processCapability(event,agents) : processStepTitle(event);
          const note = !speech && text !== basis?.reason && text !== outcome && text !== title ? text : '';
          const request = event.requested_text && event.requested_text !== text && event.requested_text !== basis?.reason
            ? event.requested_text : '';
          return <li className="run-process__step" key={key} data-step-id={event.step_id || undefined}>
            <span className="run-process__step-dot" aria-hidden="true"/>
            <div className="run-process__step-content"><header>
              <div className="run-process__heading"><small className="run-process__actor">{name}</small><strong>{title}</strong></div>
              {tool && outcome && <span data-outcome={event.status}>{outcome}</span>}</header>
              {target && <small className="run-process__target">目标 · {target}</small>}
              {basis && <div className="run-process__decision" data-decision-reason><small>判断依据</small><p>{basis.reason}</p>
                {basis.evidence.map((item,i) => {const evidence = activityDecisionEvidence(item,event,allEvents);return <blockquote key={`${item.ref}-${i}`} data-decision-evidence>
                  <small>{evidence.label}{evidence.source ? ` · ${getName(evidence.source)}` : ''}</small><p>{evidence.summary}</p>
                  <small>{evidence.notice}{evidence.missingOriginal ? '；原记录未保留' : ''}</small>
                </blockquote>;})}</div>}
              {(request || args.length > 0) && <div className="run-process__execution"><small>执行内容</small>
                {request && <p className="run-process__note">{request}</p>}
                {args.length > 0 && <dl className="run-process__arguments">{args.map(arg => <div key={arg.label}><dt>{arg.label}</dt><dd>
                  {arg.color && <i style={{background:arg.color}} aria-hidden="true"/>}{arg.value}</dd></div>)}</dl>}
              </div>}
              {(note || reason && reason !== text && reason !== basis?.reason) && <div className="run-process__notes">
                {tool && event.status && <small>反馈</small>}
                {note && <p className="run-process__note">{note}</p>}
                {reason && reason !== text && reason !== basis?.reason && <p className="run-process__note">{reason}</p>}
              </div>}
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
