/* TabBar — 底部导航栏 */
function TabBar({ tab, setTab, deviceBadge }) {
  const tabs = [
    { id: 'discover', icon: 'search', label: '附近' },
    { id: 'devices',  icon: 'home',   label: '设备', badge: deviceBadge },
    { id: 'rules',    icon: 'list',   label: '规则' },
  ];
  return (
    <div style={{
      position: 'absolute', left: 12, right: 12,
      bottom: 'calc(10px + env(safe-area-inset-bottom, 0px))',
      background: 'rgba(18,17,22,0.88)', backdropFilter: 'blur(24px) saturate(140%)',
      border: '1px solid rgba(255,255,255,0.09)', borderRadius: 24,
      padding: 8, display: 'flex', gap: 4,
      boxShadow: '0 16px 40px rgba(0,0,0,0.5)',
    }}>
      {tabs.map(t => {
        const active = tab === t.id;
        return (
          <button key={t.id} onClick={() => setTab(t.id)} style={{
            flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 3,
            padding: '8px 4px', borderRadius: 16,
            background: active ? 'rgba(255,255,255,0.07)' : 'transparent',
            color: active ? '#fff' : 'rgba(255,255,255,0.38)',
            border: 'none', cursor: 'pointer', position: 'relative', fontFamily: 'inherit',
          }}>
            <Icon name={t.icon} size={18}/>
            <span style={{ fontSize: 10, fontWeight: 500 }}>{t.label}</span>
            {t.badge > 0 && !active && (
              <span style={{ position: 'absolute', top: 6, right: '26%',
                minWidth: 16, height: 16, padding: '0 4px', borderRadius: 999,
                background: LIME, color: '#0B0B0E', fontSize: 10, fontWeight: 600,
                display: 'flex', alignItems: 'center', justifyContent: 'center' }}>{t.badge}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}

window.TabBar = TabBar;
