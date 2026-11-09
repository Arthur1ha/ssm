/* Shared conversation hierarchy: speech bubbles and expandable process records.
 * Domain-specific cards are supplied by the surrounding feed. */
function ChatMessages({ entries, agents = [], defaultAgent = 'cloud_orchestrator',
  training, onReviseRule, renderEntry, scrollTargetRef, scrollKey, scrollTrigger = entries, autoScroll = true }) {
  const bottomRef = React.useRef(null);
  const getName = unitId => getAgentDisplayName(agents.find(agent => agent.unit_id === unitId) || unitId);
  const rows = [];
  for (let index = 0; index < entries.length; index++) {
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
          const finished = entries.some(item => item.requestId === e.requestId
            && item.type === 'ai' && item.final);
          const latest = e.items[e.items.length - 1];
          const search = [...e.items].reverse().find(item => item.stage?.startsWith('search_'));
          return <details key={`process-${e.requestId}-${i}`} className="run-process-summary"
            open={!finished && search ? true : undefined}>
            <summary>
              <span className="run-process-summary__marker" aria-hidden="true"/>
              <span className="run-process-summary__text">{finished
                ? `处理过程 · ${e.items.length} 条记录` : `正在处理 · ${latest.text}`}</span>
              <span className="run-process-summary__toggle" aria-hidden="true"/>
            </summary>
            <div className="run-process-summary__list">
              {e.items.map((item, itemIndex) => <div key={item.eventId || itemIndex}
                className="run-process-summary__item">
                <span>{getName(item.agent || defaultAgent)}</span>
                <p>{item.text}</p>
                {activityMediaUrl(item) && <img src={activityMediaUrl(item)} alt="搜索观察照片"
                  loading="lazy" style={{ maxWidth: '100%', maxHeight: 220, borderRadius: 8 }}/>}
              </div>)}
            </div>
          </details>;
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
          const oc = getAgentBubbleColor(agent);
          return (
            <div key={e.id || e.eventId || `${e.type}-${i}`} style={{ display: 'flex', justifyContent: 'flex-start',
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
        return renderEntry?.(e) || null;
      })}
      {training?.error && <p role="alert">{training.error}</p>}
      <div ref={bottomRef}/>
    </div>;
}
window.ChatMessages = ChatMessages;
