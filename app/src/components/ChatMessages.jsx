/* Shared conversation hierarchy: speech bubbles and expandable process records.
 * Domain-specific cards are supplied by the surrounding feed. */
function ChatMessages({ entries, agents = [], defaultAgent = 'cloud_orchestrator',
  training, onReviseRule, renderEntry, scrollTargetRef, scrollKey, scrollTrigger = entries, autoScroll = true }) {
  const bottomRef = React.useRef(null);
  const getName = unitId => getAgentDisplayName(agents.find(agent => agent.unit_id === unitId) || unitId);
  const rows = [], groups = new Map();
  entries.forEach((entry,index) => {
    if (entry.type !== 'process') { rows.push({entry,index}); return; }
    const key = entry.requestId || processGroupKey(entry) || entry.eventId || `record:${index}`;
    let group = groups.get(key);
    if (!group) { group = {type:'process_group',requestId:entry.requestId,key,items:[]};
      groups.set(key,group); rows.push({entry:group,index}); }
    group.items.push(entry);
  });

  React.useEffect(() => {
    if (!autoScroll) return;
    const target = scrollTargetRef?.current || bottomRef.current;
    target?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }, [scrollTrigger, scrollKey, autoScroll]);

  return <div className="chat-messages" style={{ padding: '0 16px' }}>
      {rows.map(({ entry: e, index: i }) => {
        if (e.type === 'rule') return <div key={e.id} className="chat-rule-message" style={{maxWidth:420,marginBottom:10}}>
          <TrainingPreview proposal={e.proposal} error={training?.proposalErrors?.[e.proposal.proposal_id]}
            onConfirm={training?.confirm} onCancel={training?.cancel} onRevise={onReviseRule}/>
        </div>;
        if (e.type === 'process_group') {
          const finished = entries.some(item => item.requestId === e.requestId && item.type === 'ai' && item.final);
          const contextText = e.requestId ? entries.find(item => item.type === 'user' && item.requestId === e.requestId)?.text : '';
          return <RunProcess key={`process-${e.key}`} events={e.items} agents={agents} getName={getName}
            finished={finished} contextText={contextText}/>;
        }
        if (e.type === 'user') {
          return (
            <div key={e.id || e.eventId || `${e.type}-${i}`} style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 8 }}>
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
          const agent = e.sender || e.agent || defaultAgent;
          return <AgentSpeech key={e.id || e.eventId || `${e.type}-${i}`} agent={agent} name={getName(agent)}
            target={e.type === 'peer_message' && e.target ? getName(e.target) : ''} text={e.text}
            emotion={e.emotion} eventId={e.eventId || e.id} receivedAt={e.avatarReceivedAt}/>;
        }
        return renderEntry?.(e) || null;
      })}
      {training?.error && <p role="alert">{training.error}</p>}
      <div ref={bottomRef}/>
    </div>;
}
window.ChatMessages = ChatMessages;
