/* Shared conversation hierarchy: speech bubbles and expandable process records.
 * Domain-specific cards are supplied by the surrounding feed. */
function ChatMessages({ entries, agents = [], defaultAgent = 'cloud_orchestrator',
  training, onReviseRule, renderEntry, scrollTargetRef, scrollKey, scrollTrigger = entries, autoScroll = true, busy = false }) {
  const bottomRef = React.useRef(null);
  const getName = unitId => getAgentDisplayName(agents.find(agent => agent.unit_id === unitId) || unitId);
  const rows = [], groups = new Map();
  entries.forEach((entry,index) => {
    if (entry.type !== 'process') { rows.push({entry,index}); return; }
    if (!processHasContent(entry)) return;
    const key = entry.requestId ? `request:${entry.requestId}` : processGroupKey(entry) || entry.eventId || `record:${index}`;
    let group = groups.get(key);
    if (!group) { group = {type:'process_group',requestId:entry.requestId,key,items:[]};
      groups.set(key,group); rows.push({entry:group,index}); }
    group.items.push(entry);
  });
  const latestRequest = [...entries].reverse().find(entry => entry.type === 'user');
  const pendingGroup = busy && latestRequest?.requestId != null
    ? rows.find(row => row.entry.type === 'process_group' && row.entry.requestId === latestRequest.requestId)?.entry : null;

  React.useEffect(() => {
    if (!autoScroll) return;
    const target = scrollTargetRef?.current || bottomRef.current;
    target?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }, [scrollTrigger, scrollKey, autoScroll, busy]);

  return <div className="chat-messages" style={{ padding: '0 16px' }}>
      {rows.map(({ entry: e, index: i }) => {
        if (e.type === 'rule') return <div key={e.id} className="chat-rule-message" style={{maxWidth:420,marginBottom:10}}>
          <TrainingPreview proposal={e.proposal} error={training?.proposalErrors?.[e.proposal.proposal_id]}
            onConfirm={training?.confirm} onCancel={training?.cancel} onRevise={onReviseRule}/>
        </div>;
        if (e.type === 'process_group') {
          const finished = entries.some(item => item.requestId === e.requestId && item.type === 'ai' && item.final);
          return <RunProcess key={`process-${e.key}`} events={e.items} agents={agents} getName={getName}
            finished={finished} pending={e === pendingGroup}/>;
        }
        if (e.type === 'user') {
          return (
            <div key={e.id || e.eventId || `${e.type}-${i}`} style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 8 }}>
              <div style={{ maxWidth: '78%', display: 'flex', flexDirection: 'column',
                alignItems: 'flex-end', gap: 4 }}>
                <div style={{ fontSize: 10, paddingRight: 4, color: 'var(--color-accent)',
                  opacity: 0.75, fontFamily: 'var(--font-mono)', letterSpacing: '0.04em' }}>你</div>
                <div style={{
                  padding: '10px 14px', fontSize: 14, lineHeight: 1.5, overflowWrap: 'anywhere',
                  borderRadius: 'var(--radius-card)',
                  background: 'var(--color-accent)', color: 'var(--color-bg)',
                }}>{e.text}</div>
              </div>
            </div>
          );
        }
        if (e.type === 'system' || e.isError) return <p key={e.id || e.eventId || `status-${i}`} className="chat-system-status" role="status">{processErrorText(e.text)}</p>;
        if (e.type === 'ai' || e.type === 'peer_message') {
          const agent = e.sender || e.agent || defaultAgent;
          const prior = rows.slice(0,rows.findIndex(row => row.entry === e)).reverse().find(row => row.entry.type !== 'process_group')?.entry;
          const showName = !prior || !['ai','peer_message'].includes(prior.type) || (prior.sender || prior.agent || defaultAgent) !== agent;
          return <AgentSpeech showName={showName} key={e.id || e.eventId || `${e.type}-${i}`} agent={agent} name={getName(agent)}
            target={e.type === 'peer_message' && e.target ? getName(e.target) : ''} text={e.text}/>;
        }
        return renderEntry?.(e) || null;
      })}
      {busy && !pendingGroup && <div className="chat-messages__waiting"><ProcessWaiting/></div>}
      {training?.error && <p className="chat-system-status" role="status">{processErrorText(training.error)}</p>}
      <div ref={bottomRef}/>
    </div>;
}
window.ChatMessages = ChatMessages;
