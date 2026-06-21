/* AppV2 — V2 根组件。
 * 主屏始终挂载；设备页以 position:fixed 包裹层叠在上方，
 * 进入时右滑入，返回时右滑出，避免整棵树切换导致的闪烁。
 */
const SUGGESTIONS_V2 = ['我要工作了', '帮我营造睡眠氛围', '有人来了', '我要离开了'];
const ONBOARDING_SUGGESTIONS_V2 = ['帮我看看有什么新成员？'];
const ONBOARDING_BUBBLE_V2 = '我刚醒来，还没认识这个空间里的新成员。要不要让我先四处听一听，看看谁在等着加入？';

function useHashLocal() {
  const { useState, useEffect } = React;
  const [h, setH] = useState(window.location.hash);
  useEffect(() => {
    const f = () => setH(window.location.hash);
    window.addEventListener('hashchange', f);
    return () => window.removeEventListener('hashchange', f);
  }, []);
  return h;
}

/* 路由跳转 */
function navigate(hash) { window.location.hash = hash; }

function AppV2() {
  const { useState } = React;
  const { connected, agents, loadingAgents, unitData, refreshAgents } = useSsmCore();
  const { thinking, thinkingText, send } = useSendIntent();
  const currentHash = useHashLocal();

  const [activityLog, setActivityLog] = useState([]);
  const [rulesOpen,   setRulesOpen]   = useState(false);
  const [pendingRule, setPendingRule]  = useState(null);
  const [savingRule,  setSavingRule]   = useState(false);
  const [adoptingDeviceId, setAdoptingDeviceId] = useState(null);

  /* 退出动画：保留即将消失的 uid，待动画完成后清除 */
  const [exitingUid, setExitingUid] = useState(null);

  const appendActivity = entry => setActivityLog(prev => [...prev, entry]);
  const ready = !loadingAgents;
  const onboarding = ready && agents.length === 0;
  const hasDevices = ready && agents.length > 0;
  const displayEntries = onboarding && activityLog.length === 0
    ? [{ type: 'ai', agent: 'orchestrator', text: ONBOARDING_BUBBLE_V2 }]
    : activityLog;
  const suggestions = onboarding ? ONBOARDING_SUGGESTIONS_V2 : (hasDevices ? SUGGESTIONS_V2 : []);

  const handleSend = (text, options = {}) => {
    const t = text.trim();
    if (!t) return;
    appendActivity({ type: 'user', text: t });
    send(t, {
      onMessage:     msg  => appendActivity({ type: 'ai', agent: 'orchestrator', text: msg }),
      onPendingRule: rule => setPendingRule(rule),
      onDiscoveryCandidates: (devices, msg) => {
        appendActivity({
          type: 'discovery',
          text: msg || '我听到几个新成员在打招呼，先把它们的名片递给你。',
          devices,
        });
      },
      intentHint: options.intentHint,
    });
  };

  const handleScan = () => {
    appendActivity({
      type: 'ai',
      agent: 'orchestrator',
      text: '扫码接入还在准备中。先让我用能力名片听一圈，也能找到正在等你的新成员。',
    });
  };

  const handleAdoptDevice = async candidate => {
    if (!candidate?.device_id || adoptingDeviceId) return;
    setAdoptingDeviceId(candidate.device_id);
    try {
      const resp = await fetch('/api/devices/adoptions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id: candidate.device_id }),
      });
      if (!resp.ok) throw new Error('adopt failed');
      await refreshAgents();
      appendActivity({
        type: 'ai',
        agent: 'orchestrator',
        text: `认识啦，${candidate.name || candidate.device_id} 已经加入这个空间。`,
      });
    } catch {
      appendActivity({
        type: 'ai',
        agent: 'orchestrator',
        text: '这张名片暂时接不进来，等设备在线稳定后我们再试一次。',
      });
    } finally {
      setAdoptingDeviceId(null);
    }
  };

  const handleConfirmRule = () => {
    if (!pendingRule) return;
    setSavingRule(true);
    fetch('/api/rules', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(pendingRule),
    }).then(() => { setPendingRule(null); setSavingRule(false); })
      .catch(() => setSavingRule(false));
  };

  /* 返回主屏：先播退出动画再实际跳转 */
  const handleBack = (uid) => {
    setExitingUid(uid);
    navigate('#');
    setTimeout(() => setExitingUid(null), 240);
  };

  /* 当前激活的设备 uid（来自 hash） */
  const m = currentHash.match(/^#\/devices\/([^/]+)$/);
  const activeUid = m ? m[1] : null;

  /* 显示中的 uid：激活态 or 正在退出的那个 */
  const displayUid = activeUid || exitingUid;
  const isExiting  = !activeUid && exitingUid !== null;

  const displayDevice = displayUid
    ? (agents.find(a => a.unit_id === displayUid) || { unit_id: displayUid, name: displayUid })
    : null;

  return (
    <>
      {/* ── 主屏（始终挂载，不受路由影响） ── */}
      <div style={{
        position: 'fixed', inset: 0,
        background: 'var(--bg-gradient)', color: '#fff',
        fontFamily: 'var(--font-sans)', paddingTop: 'env(safe-area-inset-top,0px)',
        display: 'flex', flexDirection: 'column',
      }}>
        {/* 头部 */}
        <div style={{
          padding: '12px 16px', display: 'flex',
          alignItems: 'center', justifyContent: 'space-between', flexShrink: 0,
          borderBottom: '1px solid var(--color-border)',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span style={{ fontSize: 17, fontWeight: 700, letterSpacing: '0.08em' }}>SSM</span>
            <span style={{ fontSize: 9, color: 'var(--color-accent)', letterSpacing: '0.2em' }}>V2</span>
            <div style={{
              width: 7, height: 7, borderRadius: '50%',
              background: connected ? 'var(--color-accent)' : 'var(--color-danger)',
              boxShadow: connected ? '0 0 8px var(--color-online-glow)' : 'none',
            }}/>
          </div>
          <button onClick={() => setRulesOpen(true)} className="btn">
            <Icon name="list" size={13}/> 规则
          </button>
        </div>

        {/* 可滚动主体 */}
        <div style={{ flex: 1, overflowY: 'auto' }}>
          {hasDevices && <DevicesScreen agents={agents} unitData={unitData}/>}

          {activityLog.length > 0 && (
            <div style={{ padding: '16px 20px 8px', marginTop: 4 }}>
              <span style={{ fontSize: 13, color: 'var(--color-text-dim)', fontWeight: 500 }}>活动</span>
            </div>
          )}

          <ActivityFeed
            entries={displayEntries}
            thinking={thinking}
            thinkingText={thinkingText}
            onAdoptDevice={handleAdoptDevice}
            adoptingDeviceId={adoptingDeviceId}
          />

          {pendingRule && (
            <div style={{ padding: '0 16px 10px' }}>
              <div style={{
                background: 'var(--color-accent-dim)',
                border: '1px solid rgba(200,255,62,0.22)', borderRadius: 18, padding: '14px 16px',
              }}>
                <div style={{ fontSize: 12, color: '#C8FF3E', fontWeight: 600, marginBottom: 8 }}>
                  规则预览 · 确认保存？</div>
                <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 4 }}>{pendingRule.name}</div>
                <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.4)', fontFamily: 'monospace', marginBottom: 12 }}>
                  当 {pendingRule.trigger?.tag}.{pendingRule.trigger?.event}
                  {' → '}{pendingRule.action?.tag} / {pendingRule.action?.cmd}
                </div>
                <div style={{ display: 'flex', gap: 8 }}>
                  <button onClick={() => setPendingRule(null)} style={{
                    flex: 1, padding: '9px 0', borderRadius: 999, fontSize: 13,
                    background: 'var(--color-surface-2)', border: '1px solid var(--color-border)',
                    color: 'var(--color-text-muted)', cursor: 'pointer', fontFamily: 'inherit',
                  }}>取消</button>
                  <button onClick={handleConfirmRule} disabled={savingRule} style={{
                    flex: 2, padding: '9px 0', borderRadius: 999, fontSize: 13, fontWeight: 600,
                    background: 'var(--color-accent)', border: 'none', color: 'var(--color-bg)',
                    cursor: 'pointer', fontFamily: 'inherit',
                  }}>
                    {savingRule ? '保存中...' : '确认保存'}
                  </button>
                </div>
              </div>
            </div>
          )}
          <div style={{ height: 16 }}/>
        </div>

        {/* 输入栏 */}
        <div style={{
          flexShrink: 0, background: 'var(--color-bar)',
          backdropFilter: 'var(--glass-blur)', WebkitBackdropFilter: 'var(--glass-blur)',
          borderTop: '1px solid var(--color-border)',
        }}>
          {activityLog.every(e => e.type !== 'user') && !thinking && (
            <div style={{ padding: '8px 12px 0', display: 'flex', gap: 8, overflowX: 'auto', scrollbarWidth: 'none' }}>
              {suggestions.map(s => (
                <button key={s} onClick={() => handleSend(s, onboarding ? { intentHint: 'discover_devices' } : {})} style={{
                  flexShrink: 0, padding: '8px 16px', borderRadius: 'var(--radius-card)',
                  background: 'var(--color-surface-2)', border: '1px solid var(--color-border-strong)',
                  color: 'var(--color-text-muted)', fontSize: 13, cursor: 'pointer',
                  fontFamily: 'inherit', whiteSpace: 'nowrap',
                  WebkitTapHighlightColor: 'transparent',
                }}>{s}</button>
              ))}
            </div>
          )}
          <MainInputBar
            onSend={handleSend}
            thinking={thinking}
            placeholder={onboarding ? '问我有哪些新成员可以接入…' : undefined}
            leadingAction={onboarding ? { icon: 'scan', label: '扫码接入', onClick: handleScan } : null}
          />
        </div>

        <RulesDrawer open={rulesOpen} onClose={() => setRulesOpen(false)}/>
      </div>

      {/* ── 设备页：叠在主屏之上，CSS 滑入/滑出 ── */}
      {displayUid && (
        <div
          key={displayUid}
          style={{
            position: 'fixed', inset: 0,
            animation: isExiting
              ? 'page-slide-out 0.22s cubic-bezier(0.55,0,1,0.45) forwards'
              : 'page-slide-in  0.25s cubic-bezier(0.25,0.46,0.45,0.94)',
          }}
        >
          <FsmDevicePage
            unitId={displayUid}
            device={displayDevice}
            liveState={unitData[displayUid]}
            onBack={() => handleBack(displayUid)}
          />
        </div>
      )}
    </>
  );
}
window.AppV2 = AppV2;
