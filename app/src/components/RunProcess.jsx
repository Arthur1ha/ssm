/* One disclosure for chat and activity, backed only by public execution facts. */
function ObservationPhoto({event}) {
  const url = activityMediaUrl(event);
  const ts = Number(event.observation?.as_of ?? event.as_of ?? event.media_ref?.ts);
  const time = Number.isFinite(ts) && ts > 0 ? new Date(ts < 1e11 ? ts * 1000 : ts)
    .toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '';
  const caption = (processUserText(event.observation?.scene_summary || event.observation_summary || event.text) || '设备观察照片').slice(0,80);
  return url ? <a className="process-photo" href={url} target="_blank" rel="noopener noreferrer"
    aria-label="打开观察照片"><img src={url} alt={caption} loading="lazy"/>
    <small>{time ? `查看观察照片 · ${time}` : '查看观察照片'} ↗</small></a> : null;
}
function AgentSpeech({agent, name, target, text, compact = false, showName = true, children}) {
  return <div className={`agent-speech${compact ? ' agent-speech--compact' : ''}`}
    style={{'--speaker-color':getAgentBubbleColor(agent)}}>
    <div className="agent-speech__name" hidden={!showName}>{name}{target ? ` → ${target}` : ''}</div>
    <div className="agent-speech__bubble">{processUserText(text) || processErrorText(text)}</div>{children}
  </div>;
}
function ProcessWaiting({label = '等待回复'}) {
  return <span className="run-process__waiting" role="status" aria-live="polite">
    <span className="run-process__waiting-dots" aria-hidden="true"><i/><i/><i/></span>
    <span>{label}</span>
  </span>;
}
function RunProcess({events = [], allEvents = events, agents = [], getName = getAgentDisplayName, finished = false, pending = false}) {
  const steps = processSteps(events);
  const attention = steps.filter(step => processNeedsAttention(step.event));
  const [open,setOpen] = React.useState(false);
  if (!steps.length) return null;
  // Keep every explicit participant visible even when their steps share a title.
  const participants = new Map();
  steps.forEach(({event}) => {
    const actor = event.agent_id || event.agent || event.unit_id || 'cloud_orchestrator';
    const target = event.target_unit_id && event.target_unit_id !== actor ? event.target_unit_id : '';
    const key = JSON.stringify([actor,target]);
    if (!participants.has(key)) participants.set(key,{key,actor,target,titles:[]});
    const title = processFactTitle(event,agents);
    const titles = participants.get(key).titles;
    if (title && !titles.includes(title)) titles.push(title);
  });
  const executing = steps.find(step => step.event.status === 'executing' || step.event.stage === 'dispatching' && !processOutcome(step.event));
  // This label describes a concrete step, never the outcome of the whole run.
  const summaryEvent = executing?.event || steps[steps.length - 1].event;
  const outcome = processOutcome(summaryEvent);
  const completed = outcome === '已完成';
  const status = completed ? '已完成' : ({'结果待确认':'待确认','等待你补充信息':'待补充','执行失败':'未完成'})[outcome] || outcome;
  let previousActor = null;
  return <div className="run-process" data-process>
    <details className="run-process-summary" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
      <summary aria-expanded={open}>
        <span className="run-process-summary__marker" aria-hidden="true"/>
        <span className="run-process-summary__text">{[...participants.values()].map(item =>
          <span className="run-process-summary__participant" key={item.key}>
            <strong className="run-process-summary__device">{getName(item.actor)}{item.target ? ` → ${getName(item.target)}` : ''}</strong>
            <span className="run-process-summary__action">{item.titles.join('、') || '过程详情'}</span>
          </span>
        )}</span>
        {status && !completed && !pending && <span className="run-process-summary__status"
          data-attention={Boolean(attention.length) || undefined}>{status}</span>}
        {pending && <ProcessWaiting label={executing ? '执行中' : '等待回复'}/>}
        <svg className="process-chevron" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="m4 6 4 4 4-4"/></svg>
      </summary>
      <div className="run-process__body">
        <ol className="run-process__steps">{steps.map(({key,event}) => {
          const actor = event.agent_id || event.agent || event.unit_id || 'cloud_orchestrator';
          const name = getName(actor);
          const target = event.target_unit_id && event.target_unit_id !== actor ? getName(event.target_unit_id) : '';
          const basis = activityDecisionBasis(event);
          const action = processActionDetail(event,agents);
          const snapshot = event.state_at_event;
          const card = agents.find(agent => agent.unit_id === (event.unit_id || event.agent));
          const facts = snapshot?.status === 'current' ? activityPhysicalLabel(card,snapshot.values) : '';
          const title = processFactTitle(event,agents);
          const showActor = actor !== previousActor;
          previousActor = actor;
          const notes = processResultNotes(event);
          const observations = notes.filter(note => note.kind === 'observation');
          const feedback = notes.filter(note => note.kind !== 'observation');
          const explanation = basis && processUserText(basis.reason);
          const observedAt = Number(event.observation?.as_of ?? event.as_of);
          const observationTime = Number.isFinite(observedAt) && observedAt > 0
            ? new Date(observedAt*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}) : '';
          return <li className="run-process__step" key={key}>
            <span className="run-process__step-dot" aria-hidden="true"/>
            <div className="run-process__step-content"><header>
              <div className="run-process__heading">{showActor && <small className="run-process__actor">{name}</small>}{steps.length>1 && title && <strong>{title}</strong>}</div>
              </header>
              {target && <small className="run-process__target">交给{target}</small>}
              {observations.length>0 && <div className="run-process__observation">
                {observations.map(note => <p key={note.text}>{note.text}</p>)}
                {observationTime && <small>{observationTime} 的观察</small>}
              </div>}
              <ObservationPhoto event={event}/>
              {explanation && <div className="run-process__decision" data-decision-reason><p>「{explanation}」</p>
                {basis.evidence.map((item,i) => {const evidence = activityDecisionEvidence(item,event,allEvents);
                  const notice = [evidence.notice,evidence.missingOriginal ? '原记录未保留' : ''].filter(Boolean).join('；');
                  return <blockquote key={`${item.ref}-${i}`} data-decision-evidence>
                  {(evidence.label || evidence.source) && <small>{[evidence.label,evidence.source ? getName(evidence.source) : ''].filter(Boolean).join(' · ')}</small>}<p>{processUserText(evidence.summary)}</p>
                  {notice && <small>{notice}</small>}
                </blockquote>;})}</div>}
              {action.text && <div className="run-process__execution">
                <p>{action.color && <i className="run-process__color" style={{background:action.color}} aria-hidden="true"/>}{action.text}</p>
              </div>}
              {feedback.length>0 && <div className="run-process__notes">
                {feedback.map(note => <p key={note.text} className="run-process__feedback" data-attention={note.attention || undefined}>
                  {note.text}
                </p>)}
              </div>}
              {facts && !facts.includes('待确认') && <p className="run-process__snapshot">{facts}</p>}
            </div>
          </li>;
        })}</ol>
      </div>
    </details>
  </div>;
}
Object.assign(window,{RunProcess,ObservationPhoto,AgentSpeech,ProcessWaiting});
