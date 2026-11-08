function TrainingDrawer({unitId,training,onClose,onLocate,onRevise,agents,onUnitChange}) {
  const {records,setEnabled,remove,error} = training;
  const panelRef = React.useRef(null);
  const closeRef = React.useRef(onClose);
  closeRef.current = onClose;
  React.useEffect(() => {
    const previousFocus = document.activeElement;
    panelRef.current?.querySelector('button')?.focus();
    const onKey = event => {
      if (event.key === 'Escape') closeRef.current();
      if (event.key !== 'Tab') return;
      const controls = panelRef.current?.querySelectorAll('button:not([disabled]), select');
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
  return <div className="training-overlay" onClick={onClose}><section ref={panelRef} className="management-dialog training-drawer" role="dialog" aria-modal="true" aria-label="伙伴训练" onClick={e => e.stopPropagation()}>
    <header><h2>伙伴训练</h2><button type="button" className="icon-button" onClick={onClose} aria-label="关闭伙伴训练"><Icon name="x" size={16}/></button></header>
    <div className="training-drawer__body">
    {agents && <label className="training-device-select">训练伙伴
      <select value={unitId} onChange={event => onUnitChange(event.target.value)}>
        {!agents.length && <option value="">暂无设备</option>}
        {agents.map(agent => <option key={agent.unit_id} value={agent.unit_id}>{getAgentDisplayName(agent)}</option>)}
      </select>
    </label>}
    {error && <p role="alert">{error}</p>}
    {!records.length && <p className="asset-empty">{agents && !agents.length ? '添加设备后，就可以开始伙伴训练。' : '还没有训练。选择设备状态图上的连线，告诉伙伴你希望改变什么。'}</p>}
    {records.map(record => <article key={record.teaching_id} className="training-record">
      <strong>{record.transitionLabel}</strong><p>{record.source_text || record.name}</p><p>{record.changeText}</p><p>{record.scopeText}</p>
      <small>{record.desired_status === 'removed' ? '已撤销' : record.desired_status === 'paused' ? '已暂停' : '已保存'} · {record.load_status === 'loaded' ? '已加载' : '等待设备加载'}</small>
      {!!record.execution_reasons?.length && <p>当前限制：{record.execution_reasons.map(r => r.code).join('、')}</p>}
      <div className="training-actions"><button onClick={() => onLocate(unitId,record.transition_ids?.[0])}>定位</button>
        {record.desired_status !== 'removed' && <><button onClick={() => setEnabled(record,record.desired_status !== 'enabled')}>{record.desired_status === 'enabled' ? '暂停' : '启用'}</button>
        <button onClick={() => onRevise(record)}>追改</button><button onClick={async () => { if (await remove(record)) onClose(); }}>撤销</button></>}</div>
    </article>)}
    </div>
  </section></div>;
}
window.TrainingDrawer = TrainingDrawer;
