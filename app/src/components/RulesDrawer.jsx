/* RulesDrawer — 规则管理底部抽屉 */
function RulesDrawer({ open, onClose, initialTeachingId }) {
  const [rules, setRules] = React.useState([]);
  return (
    <>
      <div className={`drawer-mask ${open ? 'is-open' : ''}`} onClick={onClose}/>
      <aside className={`space-drawer rules-drawer ${open ? 'is-open' : ''}`}
        style={{ visibility: open ? 'visible' : 'hidden' }} inert={open ? undefined : ''}
        role="dialog" aria-modal="true" aria-hidden={!open} aria-labelledby="rules-drawer-title">
        <div className="space-drawer__handle" aria-hidden="true"/>
        <header className="space-drawer__header">
          <div>
            <h2 id="rules-drawer-title">教导与自动规则</h2>
            {rules.length > 0 && <small>{rules.length} 条已记住</small>}
          </div>
          <button type="button" className="btn space-drawer__close" onClick={onClose} aria-label="关闭自动化规则">
            <Icon name="x" size={16}/>
          </button>
        </header>

        {/* RulesScreen 内容 */}
        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', paddingBottom: 'max(16px, env(safe-area-inset-bottom))' }}>
          <RulesScreen embedded={true} onRulesChange={setRules} open={open} initialTeachingId={initialTeachingId}/>
        </div>
      </aside>
    </>
  );
}

window.RulesDrawer = RulesDrawer;
