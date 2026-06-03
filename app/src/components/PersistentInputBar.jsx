/* PersistentInputBar — 主页常驻输入栏，点击弹出 ChatSheet */
function PersistentInputBar({ onOpen }) {
  return (
    <div onClick={onOpen} style={{
      position: 'absolute', left: 12, right: 12,
      bottom: 'calc(86px + env(safe-area-inset-bottom, 0px))',
      display: 'flex', alignItems: 'center', gap: 10,
      padding: '10px 10px 10px 16px',
      background: 'rgba(22,21,28,0.94)',
      backdropFilter: 'blur(28px) saturate(140%)',
      border: '1px solid rgba(255,255,255,0.09)',
      borderRadius: 999,
      cursor: 'text',
      boxShadow: '0 4px 24px rgba(0,0,0,0.35)',
      zIndex: 10,
    }}>
      <span style={{ fontSize: 17, lineHeight: 1 }}>◐</span>
      <span style={{ flex: 1, fontSize: 14, color: 'rgba(255,255,255,0.3)', userSelect: 'none' }}>
        告诉我想要什么…
      </span>
      <div style={{ width: 34, height: 34, borderRadius: 999, flexShrink: 0,
        background: `${LIME}15`, border: `1px solid ${LIME}33`,
        display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Icon name="arrow" size={14} color={LIME}/>
      </div>
    </div>
  );
}

window.PersistentInputBar = PersistentInputBar;
