/* App — 根组件。
 * 主屏始终挂载；设备页以 position:fixed 包裹层叠在上方，
 * 进入时右滑入，返回时右滑出，避免整棵树切换导致的闪烁。
 */
const SUGGESTIONS = ['我要工作了', '帮我营造睡眠氛围', '有人来了', '我要离开了'];

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

function App({ session, logout, refreshSession }) {
  const { useState, useEffect, useRef } = React;
  const {
    connected, agents, loadingAgents, agentsError, unitData, refreshAgents,
  } = useSsmCore();
  const recentEvents = useSpaceActivity(agents);
  const { activity: currentActivity, displayActivity, events: activityEvents, ending: activityEnding,
    error: activityError, endActivity } = useActivityTimeline();
  const { thinking, send } = useSendIntent();
  const currentHash = useHashLocal();

  const [activityLog, setActivityLog] = useState([]);
  const [activityHistory, setActivityHistory] = useState([]);
  const lastVisibleActivity = useRef(null);
  const seenReplies = useRef(new Set());
  const seenPeerMessages = useRef(new Set());
  const completedRequests = useRef(new Set());
  const traceRequests = useRef(new Map());
  const pendingReplies = useRef([]);
  const nextRequestId = useRef(0);
  const [rulesOpen,   setRulesOpen]   = useState(false);
  const [inviteOpen,  setInviteOpen]  = useState(false);
  const [spaceOpen,   setSpaceOpen]   = useState(false);
  const [peopleOpen,  setPeopleOpen]  = useState(false);
  const [deviceScanOpen, setDeviceScanOpen] = useState(false);
  const [deviceNotice, setDeviceNotice] = useState('');
  const [activityQuestion, setActivityQuestion] = useState(null);
  const [pendingRule, setPendingRule]  = useState(null);
  const [savingRule,  setSavingRule]   = useState(false);

  const visibleActivityId = displayActivity?.activity_id || activityEvents[0]?.activity_id;
  useEffect(() => {
    if (!visibleActivityId) return;
    const previous = lastVisibleActivity.current;
    if (previous && previous.id !== visibleActivityId) {
      setActivityHistory(history => [...history.filter(item => item.id !== previous.id), {
        ...previous,
        activity: previous.activity ? { ...previous.activity, status: 'ended' } : null,
      }].slice(-3));
    }
    lastVisibleActivity.current = {
      id: visibleActivityId, activity: displayActivity, events: activityEvents,
    };
  }, [visibleActivityId, displayActivity, activityEvents]);

  /* 退出动画：保留即将消失的 uid，待动画完成后清除 */
  const [exitingUid, setExitingUid] = useState(null);

  const appendActivity = entry => setActivityLog(prev => [...prev, entry]);
  const insertBeforeRequestEnd = (entries, requestId, row) => {
    const endIndex = entries.findIndex(entry => entry.requestId === requestId
      && (entry.terminal || entry.type === 'ai' && entry.final));
    if (endIndex >= 0) return [...entries.slice(0, endIndex), row, ...entries.slice(endIndex)];
    for (let index = entries.length - 1; index >= 0; index--) {
      if (entries[index].requestId === requestId) {
        return [...entries.slice(0, index + 1), row, ...entries.slice(index + 1)];
      }
    }
    return [...entries, row];
  };
  const appendProcess = (requestId, agent, text, eventId) => {
    if (!text) return;
    setActivityLog(prev => {
      if (eventId && prev.some(entry => entry.type === 'process'
        && entry.requestId === requestId && entry.eventId === eventId)) return prev;
      return insertBeforeRequestEnd(prev, requestId,
        { type: 'process', requestId, agent, text, eventId });
    });
  };
  const finishRequest = (requestId, text, eventId) => {
    if (!text || completedRequests.current.has(requestId)) return;
    completedRequests.current.add(requestId);
    setActivityLog(prev => {
      let position = prev.length;
      for (let index = prev.length - 1; index >= 0; index--) {
        if (prev[index].requestId === requestId) { position = index + 1; break; }
      }
      return [...prev.slice(0, position),
        { type: 'process', agent: 'cloud_orchestrator', requestId,
          text: '处理结束', terminal: true, eventId: `terminal:${requestId}` },
        { type: 'ai', agent: 'cloud_orchestrator', requestId, text, final: true, eventId },
        ...prev.slice(position)];
    });
  };
  const appendReply = (item) => {
    if (!item?.text) return;
    const requestId = traceRequests.current.get(item.trace_id);
    const replyKey = item.event_id ? `${requestId || 'space'}:${item.event_id}` : '';
    if (replyKey && seenReplies.current.has(replyKey)) return;
    if (replyKey) seenReplies.current.add(replyKey);
    if (requestId && ['orchestrator', 'cloud_orchestrator'].includes(item.agent_id)) {
      finishRequest(requestId, item.text, item.event_id);
      return;
    }
    if (requestId) {
      appendProcess(requestId, item.agent_id, item.text, item.event_id);
    } else {
      appendActivity({ type: 'ai', agent: item.agent_id, text: item.text,
        eventId: item.event_id, traceId: item.trace_id });
    }
  };
  useEffect(() => {
    const onEvent = event => {
      const item = event.detail || {};
      if (item.type === 'peer_message' && item.message_id && item.text) {
        if (seenPeerMessages.current.has(item.message_id)) return;
        seenPeerMessages.current.add(item.message_id);
        if (seenPeerMessages.current.size > 100) {
          seenPeerMessages.current.delete(seenPeerMessages.current.values().next().value);
        }
        appendActivity({ type: 'peer_message', sender: item.sender, target: item.target,
          replyTo: item.reply_to, text: item.text, eventId: item.event_id });
        return;
      }
      if (['agent_progress', 'run_stage'].includes(item.type) && item.trace_id
          && item.stage !== 'finished') {
        const requestId = traceRequests.current.get(item.trace_id);
        if (requestId && completedRequests.current.has(requestId)) {
          appendProcess(requestId, item.target_unit_id || item.agent_id || 'cloud_orchestrator',
            processEventText(item), item.event_id);
        }
        return;
      }
      if (item.type !== 'agent_reply' || !item.text) return;
      if (item.scope === 'space' && item.source === 'autonomous') {
        if (item.activity_id && item.activity_id === currentActivity?.activity_id) return;
        appendReply(item);
      } else if (item.scope === 'session'
          && ['orchestrator', 'delegated_skill'].includes(item.source)) {
        if (traceRequests.current.has(item.trace_id)) appendReply(item);
        else pendingReplies.current = [...pendingReplies.current.slice(-49), item];
      }
    };
    apiClient.addEventListener('server-event', onEvent);
    return () => apiClient.removeEventListener('server-event', onEvent);
  }, [currentActivity?.activity_id]);
  const ready = !loadingAgents;
  const hasDevices = ready && agents.length > 0;
  const suggestions = hasDevices ? SUGGESTIONS : [];

  const handleSend = (text, options = {}) => {
    const t = text.trim();
    if (!t || thinking) return;
    const requestId = ++nextRequestId.current;
    setActivityLog(prev => [...prev,
      { type: 'user', text: t, requestId },
      { type: 'process', agent: 'cloud_orchestrator', requestId,
        text: '已收到请求', eventId: `initial:${requestId}` },
    ]);
    send(t, {
      onMessage: (msg, agent, event) => appendReply({ ...event, text: msg,
        agent_id: agent || 'cloud_orchestrator' }),
      onProgress: event => {
        if (event.stage === 'finished') return;
        appendProcess(requestId,
          event.target_unit_id || event.agent_id || 'cloud_orchestrator',
          processEventText(event), event.event_id);
      },
      onStatus: msg => finishRequest(requestId, msg),
      onTraceId: (traceId, eventId) => {
        traceRequests.current.set(traceId, requestId);
        if (eventId) setActivityLog(prev => prev.map(entry =>
          entry.type === 'user' && entry.requestId === requestId
            ? { ...entry, intentEventId: eventId } : entry));
        if (traceRequests.current.size > 20) traceRequests.current.delete(traceRequests.current.keys().next().value);
        const pending = pendingReplies.current;
        pendingReplies.current = pending.filter(item => item.trace_id !== traceId);
        pending.filter(item => item.trace_id === traceId).forEach(appendReply);
      },
      onPendingRule: rule => {
        if (session.actor === 'admin') {
          finishRequest(requestId, '规则已经准备好，请确认是否保存。');
          setPendingRule(rule);
        } else {
          finishRequest(requestId, '临时体验不会保存长期规则，你仍可以直接控制现有设备。');
        }
      },
      intentHint: options.intentHint,
    });
  };

  const handleConfirmRule = () => {
    if (!pendingRule) return;
    setSavingRule(true);
    apiClient.request('/api/rules', { method: 'POST', body: pendingRule })
      .then(() => { setPendingRule(null); setSavingRule(false); })
      .catch(error => {
        appendActivity({ type: 'ai', agent: 'orchestrator', text: apiErrorText(error) });
        setSavingRule(false);
      });
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
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
            <div style={{
              width: 7, height: 7, borderRadius: '50%',
              background: connected ? 'var(--color-accent)' : 'var(--color-danger)',
              boxShadow: connected ? '0 0 8px var(--color-online-glow)' : 'none',
              flexShrink: 0,
            }}/>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 15, fontWeight: 650, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{session.space_name || '当前空间'}</div>
              <div style={{ fontSize: 10, color: 'var(--color-text-dim)' }}>{connected ? '空间已连接' : '连接中断'}</div>
            </div>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
            {session.actor === 'visitor' && <SessionBadge session={session}/>}
            {session.actor === 'admin' && <div className="feedback-button"><button type="button" className="btn device-scan-trigger"
              onClick={() => { setDeviceNotice(''); setDeviceScanOpen(true); }} aria-label="扫描设备二维码添加设备">
              <Icon name="scan" size={16}/><span>扫描设备</span>
            </button>{deviceNotice && <small className="action-feedback action-feedback--ok" role="status" title={deviceNotice}>{deviceNotice}</small>}</div>}
            {session.actor === 'admin' && <SpaceMenu
              onShareSpace={() => setInviteOpen(true)} onRules={() => setRulesOpen(true)}
              onSpaceSettings={() => setSpaceOpen(true)} onPeople={() => setPeopleOpen(true)} onExit={logout}/>}
          </div>
        </div>

        {/* 可滚动主体 */}
        <div style={{ flex: 1, overflowY: 'auto' }}>
          {agentsError && (
            <div style={{ padding: '14px 16px 0' }}>
              <div className="feedback-button" style={{ alignItems: 'flex-start' }}>
                <button className="btn" onClick={refreshAgents} disabled={loadingAgents}>
                  {loadingAgents ? '重试中…' : '重试'}
                </button>
                <small className="action-feedback action-feedback--error" role="alert" title={agentsError}>{agentsError}</small>
              </div>
            </div>
          )}
          {hasDevices && <>
            <DevicesScreen spaceName={session.space_name} agents={agents} unitData={unitData}
              connected={connected} recentEvents={recentEvents}/>
          </>}
          {!loadingAgents && !agentsError && !hasDevices &&
            <DeviceEmptyState isAdmin={session.actor === 'admin'} onAddDevice={() => setDeviceScanOpen(true)}/>}

          {(activityLog.length > 0 || activityHistory.length > 0 || displayActivity || activityEvents.length > 0 || activityError) && (
            <div style={{ padding: '16px 20px 8px', marginTop: 4 }}>
              <span style={{ fontSize: 13, color: 'var(--color-text-dim)', fontWeight: 500 }}>对话</span>
            </div>
          )}

          <ActivityFeed entries={activityLog} agents={agents} activity={displayActivity}
            activityHistory={activityHistory}
            activityEvents={activityEvents} activityEnding={activityEnding}
            activityError={activityError} onEndActivity={endActivity}
            onAnswerActivity={event => {
              setActivityQuestion(event);
              navigate(`#/devices/${encodeURIComponent(event.unit_id || 'go2')}`);
            }}/>

          {pendingRule && session.actor === 'admin' && (
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
          borderTop: '1px solid var(--color-border)',
        }}>
          {activityLog.every(e => e.type !== 'user') && !thinking && (
            <div style={{ padding: '8px 12px 0', display: 'flex', gap: 8, overflowX: 'auto', scrollbarWidth: 'none' }}>
              {suggestions.map(s => (
                <button key={s} onClick={() => handleSend(s)} style={{
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
            placeholder={
              loadingAgents
                ? '正在整理空间成员…'
                : '告诉我想让大家做什么…'
            }
          />
        </div>

        {session.actor === 'admin' && <RulesDrawer open={rulesOpen} onClose={() => setRulesOpen(false)}/>}
        {session.actor === 'admin' && <AdminInviteDrawer open={inviteOpen} onClose={() => setInviteOpen(false)}/>}
        {session.actor === 'admin' && <AdminSpaceDrawer open={spaceOpen} onClose={() => setSpaceOpen(false)} onInvite={() => { setSpaceOpen(false); setInviteOpen(true); }} onDevicesChanged={refreshAgents} onSpaceRenamed={() => { refreshSession().catch(() => {}); }}/>}
        {session.actor === 'admin' && <SpacePeopleDrawer open={peopleOpen} onClose={() => setPeopleOpen(false)}/>}
        {session.actor === 'admin' && deviceScanOpen && <DeviceScanDialog onClose={() => setDeviceScanOpen(false)} onBound={async result => {
          setDeviceScanOpen(false);
          setDeviceNotice(result.result === 'already_added' ? '设备已在空间中，原有设置已保留。' : '设备已加入当前空间。');
          await refreshAgents();
        }}/>}
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
            actor={session.actor}
            activityQuestion={activityQuestion?.unit_id === displayUid &&
              activityQuestion.activity_id === currentActivity?.activity_id ? activityQuestion : null}
          />
        </div>
      )}
    </>
  );
}
window.App = App;

function DeviceScanDialog({ onClose, onBound }) {
  const { useEffect, useRef, useState } = React;
  const [attempt, setAttempt] = useState(0);
  const [pasteOpen, setPasteOpen] = useState(false);
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const inFlight = useRef(false);

  useEffect(() => {
    const onKey = event => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const submit = async value => {
    if (inFlight.current) return;
    const raw = String(value || '').trim();
    if (!raw) { setError('请扫描或粘贴设备码'); return; }
    if (raw.includes('#invite=') || raw.includes('#token=')) {
      setError('这不是设备二维码');
      return;
    }
    inFlight.current = true;
    setBusy(true);
    setError('');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const result = await apiClient.request('/api/admin/devices/bind', { method: 'POST', body: { code: raw }, signal: controller.signal });
      await onBound(result);
    } catch (requestError) {
      setError(requestError.name === 'AbortError' ? '识别超时，请检查网络后重试'
        : requestError.status === 400 ? '这不是有效的设备二维码'
        : requestError.status === 404 ? '还未发现这台设备，请启动设备后重试'
        : apiErrorText(requestError, '设备识别失败，请重试'));
      setPasteOpen(true);
    } finally {
      clearTimeout(timeout);
      inFlight.current = false;
      setBusy(false);
    }
  };

  return <div className="device-scan-backdrop" role="presentation" onClick={onClose}>
    <section className="device-scan-dialog" role="dialog" aria-modal="true" aria-labelledby="device-scan-title" onClick={event => event.stopPropagation()}>
      <header><div><small>DEVICE ONBOARDING</small><h2 id="device-scan-title">扫码添加设备</h2></div>
        <button className="btn header-icon-btn" onClick={onClose} aria-label="关闭扫码"><Icon name="x" size={16}/></button></header>
      <p>扫描设备上的二维码标签，识别后自动加入当前空间。</p>
      {!busy && !pasteOpen && <QrScanner key={attempt} onScan={submit} hint="将设备二维码放入框内"/>}
      {pasteOpen && !busy && <form className="session-entry__form" onSubmit={event => { event.preventDefault(); submit(code); }}>
        <label htmlFor="device-code">设备码</label>
        <input id="device-code" value={code} onChange={event => setCode(event.target.value)} autoComplete="off" placeholder="粘贴设备标签中的内容"/>
        <button className="btn btn--accent" disabled={!code.trim()}>识别并添加设备</button>
        {error && <small className="action-feedback action-feedback--error" role="alert" title={error}>{error}</small>}
      </form>}
      <div className="device-scan-actions">
        <button className="btn" onClick={() => { setError(''); setPasteOpen(false); setAttempt(value => value + 1); }} disabled={busy}>重新扫码</button>
        <button className="btn" onClick={() => setPasteOpen(value => !value)} disabled={busy}>{pasteOpen ? '返回摄像头' : '粘贴设备码'}</button>
      </div>
      {busy && <small className="action-feedback" role="status">正在识别设备…</small>}
      {!pasteOpen && error && <small className="action-feedback action-feedback--error" role="alert" title={error}>{error}</small>}
    </section>
  </div>;
}
