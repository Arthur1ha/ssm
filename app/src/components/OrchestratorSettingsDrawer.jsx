function OrchestratorSettingsDrawer({ open, onClose }) {
  const { useEffect } = React;
  useEffect(() => {
    if (!open) return undefined;
    const closeOnEscape = event => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [open, onClose]);

  return <>
    <div className={`drawer-mask ${open ? 'is-open' : ''}`} onClick={onClose}/>
    <aside className={`management-dialog space-drawer orchestrator-settings-drawer ${open ? 'is-open' : ''}`}
      role="dialog" aria-modal="true" aria-hidden={!open} inert={open ? undefined : ''} style={{ visibility: open ? 'visible' : 'hidden' }} aria-labelledby="orchestrator-settings-title">
      <header className="space-drawer__header">
        <div><h2 id="orchestrator-settings-title">{getAgentDisplayName('cloud_orchestrator')}管理</h2></div>
        <button type="button" className="icon-button space-drawer__close" onClick={onClose} aria-label="关闭管家管理">
          <Icon name="x" size={16}/>
        </button>
      </header>
      <div className="space-drawer__body">
        <section className="asset-section orchestrator-settings-drawer__intro">
          <div className="asset-section__heading"><div><h3>协调空间中的智能体</h3></div></div>
          <p>管家负责理解请求、安排任务并说明结果。这里调整它的表达和判断偏好，不改变设备的权限与动作规则。</p>
        </section>
        <section className="asset-section orchestrator-settings-drawer__profile">
          <div className="asset-section__heading"><div><h3>人格与特质</h3></div><span>保存后下次对话生效</span></div>
          <AgentPersonalitySettings agentId="cloud_orchestrator" active={open}/>
        </section>
      </div>
    </aside>
  </>;
}
window.OrchestratorSettingsDrawer = OrchestratorSettingsDrawer;
