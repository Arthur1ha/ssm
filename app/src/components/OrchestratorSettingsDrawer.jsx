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
        <section className="asset-section agent-management-profile orchestrator-settings-drawer__profile">
          <p className="agent-management-intro">理解请求，协调空间中的设备。</p>
          <AgentPersonalitySettings agentId="cloud_orchestrator" active={open}/>
        </section>
      </div>
    </aside>
  </>;
}
window.OrchestratorSettingsDrawer = OrchestratorSettingsDrawer;
