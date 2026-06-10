/* RulesDrawer — 规则管理底部抽屉 */
function RulesDrawer({ open, onClose }) {
  return (
    <>
      {/* 背景遮罩 */}
      <div onClick={onClose} style={{
        position: 'fixed', inset: 0, zIndex: 200,
        background: 'rgba(0,0,0,0.55)',
        opacity: open ? 1 : 0,
        transition: 'opacity 0.3s',
        pointerEvents: open ? 'auto' : 'none',
      }}/>

      {/* 抽屉主体 */}
      <div style={{
        position: 'fixed', left: 0, right: 0, bottom: 0,
        height: '75vh',
        zIndex: 201,
        background: '#0F0F14',
        borderRadius: '22px 22px 0 0',
        border: '1px solid rgba(255,255,255,0.08)',
        boxShadow: '0 -20px 60px rgba(0,0,0,0.5)',
        transform: open ? 'translateY(0)' : 'translateY(100%)',
        transition: 'transform 0.38s cubic-bezier(0.32, 0.72, 0, 1)',
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
      }}>
        {/* 拖动条 */}
        <div style={{ padding: '10px 0 2px', display: 'flex', justifyContent: 'center', flexShrink: 0 }}>
          <div style={{ width: 36, height: 4, borderRadius: 2, background: 'rgba(255,255,255,0.12)' }}/>
        </div>

        {/* 头部 */}
        <div style={{
          padding: '6px 20px 10px', display: 'flex', alignItems: 'center',
          justifyContent: 'space-between', flexShrink: 0,
          borderBottom: '1px solid rgba(255,255,255,0.05)',
        }}>
          <span style={{ fontSize: 16, fontWeight: 400 }}>自动化规则</span>
          <button onClick={onClose} style={{
            width: 30, height: 30, borderRadius: '50%',
            border: '1px solid rgba(255,255,255,0.09)',
            background: 'rgba(255,255,255,0.06)', color: 'rgba(255,255,255,0.6)',
            cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 0,
          }}>
            <Icon name="x" size={14}/>
          </button>
        </div>

        {/* RulesScreen 内容 */}
        <div style={{ flex: 1, overflowY: 'auto' }}>
          <RulesScreen embedded={true}/>
        </div>
      </div>
    </>
  );
}

window.RulesDrawer = RulesDrawer;
