function TrainingDrawer({unitId,training,onClose,onRevise,agents}) {
  const {records,allRecords,loadAll,remove,error} = training;
  const [removing, setRemoving] = React.useState(null);
  const panelRef = React.useRef(null);
  const closeRef = React.useRef(onClose);
  closeRef.current = onClose;
  React.useEffect(() => { if (agents) loadAll?.(agents); }, [agents, loadAll]);
  React.useEffect(() => {
    const previousFocus = document.activeElement;
    panelRef.current?.querySelector('button')?.focus();
    const onKey = event => {
      if (event.key === 'Escape') closeRef.current();
      if (event.key !== 'Tab') return;
      const controls = panelRef.current?.querySelectorAll('button:not([disabled])');
      if (!controls?.length) return;
      if (event.shiftKey && document.activeElement === controls[0]) {
        event.preventDefault(); controls[controls.length - 1].focus();
      } else if (!event.shiftKey && document.activeElement === controls[controls.length - 1]) {
        event.preventDefault(); controls[0].focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, []);
  const visibleRecords = (agents ? allRecords || [] : records).filter(record => record.desired_status === 'enabled');
  const groups = agents ? agents.filter(agent => visibleRecords.some(record => record.unit_id === agent.unit_id))
    : [{unit_id:unitId}];
  return <div className="training-overlay" onClick={onClose}><section ref={panelRef} className="management-dialog training-drawer" role="dialog" aria-modal="true" aria-label="设备规则" onClick={e => e.stopPropagation()}>
    <header><h2>设备规则</h2><button type="button" className="icon-button" onClick={onClose} aria-label="关闭设备规则"><Icon name="x" size={16}/></button></header>
    <div className="training-drawer__body">
    {error && <p role="alert">{error}</p>}
    {!visibleRecords.length && <p className="asset-empty">{agents && !agents.length ? '添加设备后，直接在对话中告诉它你的要求。' : '还没有规则。直接在对话中告诉设备你希望它怎么做。'}</p>}
    {groups.map(agent => <section key={agent.unit_id} className="training-device-group">
      {visibleRecords.length > 0 && <h3>{getAgentDisplayName(agent.unit_id)}</h3>}
      {visibleRecords.filter(record => record.unit_id === agent.unit_id || (!agents && !record.unit_id)).map(record => <article key={record.teaching_id} className="training-record">
        <p>{record.summaryText || record.name || record.source_text}</p>
        <TrainingRuleSteps record={record}/>
        <div className="training-actions">
          <button type="button" onClick={() => onRevise(record)}>在对话中修改</button>
          <button type="button" disabled={removing === record.teaching_id} onClick={async () => {
            setRemoving(record.teaching_id);
            try { await remove(record); } finally { setRemoving(null); }
          }}>{removing === record.teaching_id ? '正在删除…' : '删除'}</button>
        </div>
      </article>)}
    </section>)}
    </div>
  </section></div>;
}
window.TrainingDrawer = TrainingDrawer;
