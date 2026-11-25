/* App — 根组件。
 * 主屏始终挂载；设备页以 position:fixed 包裹层叠在上方，
 * 进入时右滑入，返回时右滑出，避免整棵树切换导致的闪烁。
 */
const SUGGESTIONS = [
  '让机器狗挥挥手，再把灯调成暖白色',
  '我要干活了，你们自己玩吧',
  '你们两玩石头剪刀布',
  '你们两玩送货游戏',
];

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
  const { useState, useEffect, useRef, useMemo } = React;
  const {
    connected, agents, loadingAgents, hasLoadedAgents, agentsError, unitData, refreshAgents,
  } = useSsmCore();
  const recentEvents = useSpaceActivity(agents);
  const { activity: currentActivity, displayActivity, events: activityEvents, ending: activityEnding,
    error: activityError, endActivity } = useActivityTimeline();
  useEffect(() => { companionPresentation.setActivity(currentActivity); }, [currentActivity]);
  const { thinking, send } = useSendIntent();
  const currentHash = useHashLocal();

  const [activityLog, setActivityLog] = useSessionTranscript(session.session_id, 'home');
  const [activityHistory, setActivityHistory] = useState([]);
  const lastVisibleActivity = useRef(null);
  const seenReplies = useRef(new Set());
  const seenPeerMessages = useRef(new Set());
  const completedRequests = useRef(new Set());
  const supersededRequests = useRef(new Set());
  const traceRequests = useRef(new Map());
  const pendingReplies = useRef([]);
  const nextRequestId = useRef(Date.now());
  const [trainingOpen,   setTrainingOpen]   = useState(false);
  const [inviteOpen,  setInviteOpen]  = useState(false);
  const [spaceOpen,   setSpaceOpen]   = useState(false);
  const [orchestratorOpen, setOrchestratorOpen] = useState(false);
  const [peopleOpen,  setPeopleOpen]  = useState(false);
  const [deviceScanOpen, setDeviceScanOpen] = useState(false);
  const deviceBinding = useRef(false);
  const { toast, showToast, dismissToast } = useToast();
  const [activityQuestion, setActivityQuestion] = useState(null);
  const training = useTraining('', {sessionId:session.session_id,canEdit:session.actor === 'admin'});
  const chatRows = useMemo(() => trainingTimeline(activityLog, session.actor === 'admin' ? training.proposals : []), [activityLog,training.proposals,session.actor]);
  useEffect(() => { if (session.actor === 'admin') training.loadAll(agents); }, [agents.map(agent => agent.unit_id).join(','), session.session_id]);
  const [trainingSelection, setTrainingSelection] = useState(null);
  const [trainingLocation, setTrainingLocation] = useState(null);
  useEffect(() => { setTrainingSelection(null); setTrainingLocation(null); }, [training.resetVersion]);
  const [, setAgentNamesRevision] = useState(0);

  const handleDeviceScan = async value => {
    if (deviceBinding.current) return;
    setDeviceScanOpen(false);
    const code = String(value || '').trim();
    if (!/^ssm-device:v1:(esp32_desk|go2):[A-Za-z0-9_-]+$/.test(code)) {
      showToast('请使用空间管理中的设备标签');
      return;
    }
    deviceBinding.current = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const result = await apiClient.request('/api/admin/devices/bind', {
        method: 'POST', body: { code }, signal: controller.signal,
      });
      showToast(result.result === 'already_added' ? '空间已有，无需重复添加' : '添加成功',
        result.result === 'added' ? { action: {
          label: '撤销', successMessage: '已撤销添加', errorMessage: '撤销失败，请重试',
          onClick: async () => {
            await apiClient.request(`/api/admin/devices/${encodeURIComponent(result.device_id)}`, { method: 'DELETE' });
            await refreshAgents();
          },
        } } : {});
      refreshAgents();
    } catch (error) {
      showToast(error.name === 'AbortError' ? '添加超时，请检查设备列表'
        : error.status === 400 ? '设备标签已失效，请重新获取'
        : error.status === 404 ? '未发现设备，请确认已启动'
        : apiErrorText(error, '添加失败，请稍后重试'));
    } finally {
      clearTimeout(timeout);
      deviceBinding.current = false;
    }
  };

  useEffect(() => {
    const onNamesChanged = () => setAgentNamesRevision(value => value + 1);
    window.addEventListener('agent-names-changed', onNamesChanged);
    apiClient.request('/api/agents/names').then(setAgentDisplayNames).catch(() => {});
    return () => window.removeEventListener('agent-names-changed', onNamesChanged);
  }, []);

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
      && entry.type === 'ai' && entry.final);
    if (endIndex >= 0) return [...entries.slice(0, endIndex), row, ...entries.slice(endIndex)];
    for (let index = entries.length - 1; index >= 0; index--) {
      if (entries[index].requestId === requestId) {
        return [...entries.slice(0, index + 1), row, ...entries.slice(index + 1)];
      }
    }
    return [...entries, row];
  };
  const appendProcess = (requestId, agent, text, eventId, event = {}) => {
    text = typeof event.text === 'string' && event.text.trim() ? event.text.trim() : text;
    if (!processHasContent({...event,text})) return;
    setActivityLog(prev => {
      const identity = event.source_event_id || eventId;
      const existing = identity ? prev.findIndex(entry => entry.type === 'process'
        && entry.requestId === requestId && (entry.source_event_id || entry.eventId) === identity) : -1;
      if (existing >= 0) return prev.map((entry,index) => index === existing
        ? mergeProcessFact(entry,{...processRecord(event),text}) : entry);
      return insertBeforeRequestEnd(prev, requestId,
        { ...processRecord(event), type: 'process', requestId, agent, text, eventId });
    });
  };
  const finishRequest = (requestId, text, eventId, emotion) => {
    if (!text || completedRequests.current.has(requestId)) return;
    completedRequests.current.add(requestId);
    setActivityLog(prev => {
      let position = prev.length;
      for (let index = prev.length - 1; index >= 0; index--) {
        if (prev[index].requestId === requestId) { position = index + 1; break; }
      }
      return [...prev.slice(0, position),
        { type: 'ai', agent: 'cloud_orchestrator', requestId, text, final: true, eventId, emotion },
        ...prev.slice(position)];
    });
  };
  const appendReply = (item) => {
    if (!item?.text) return;
    const requestId = traceRequests.current.get(item.trace_id);
    if (supersededRequests.current.has(requestId)) return;
    const replyIdentity = item.source_event_id || item.event_id;
    const replyKey = replyIdentity ? `${item.trace_id || 'space'}:${replyIdentity}` : '';
    if (replyKey && seenReplies.current.has(replyKey)) return;
    if (replyKey) seenReplies.current.add(replyKey);
    if (requestId && ['orchestrator', 'cloud_orchestrator'].includes(item.agent_id)) {
      finishRequest(requestId, item.text, item.event_id, item.emotion);
      return;
    }
    if (requestId) {
      setActivityLog(prev => insertBeforeRequestEnd(prev, requestId,
        { type: 'ai', agent: item.agent_id, requestId, text: item.text,
          eventId: item.event_id, emotion:item.emotion }));
    } else {
      appendActivity({ type: 'ai', agent: item.agent_id, text: item.text,
        eventId: item.event_id, traceId: item.trace_id, emotion:item.emotion });
    }
  };
  useEffect(() => {
    const onEvent = event => {
      const item = event.detail || {};
      // Activity-owned progress and media are persisted in its timeline.
      if (item.scope === 'space' && item.activity_id) return;
      if (item.type === 'agent_progress' && item.scope === 'space'
          && item.source === 'object_search' && item.run_id) {
        appendProcess(`search-${item.run_id}`, 'go2', item.stage === 'narrated' ? '' : item.text || '', item.event_id,
          { ...item, task_id: null });
        return;
      }
      if (item.type === 'peer_message' && item.message_id && item.text) {
        if (seenPeerMessages.current.has(item.message_id)) return;
        seenPeerMessages.current.add(item.message_id);
        if (seenPeerMessages.current.size > 100) {
          seenPeerMessages.current.delete(seenPeerMessages.current.values().next().value);
        }
        appendActivity({ type: 'peer_message', sender: item.sender, target: item.target,
          replyTo: item.reply_to, text: item.text, eventId: item.event_id, emotion:item.emotion });
        return;
      }
      if (['agent_progress', 'run_stage'].includes(item.type) && item.trace_id
          && item.stage !== 'finished') {
        const requestId = traceRequests.current.get(item.trace_id);
        if (supersededRequests.current.has(requestId)
            && item.fact_kind !== 'result' && !item.public_result) return;
        if (requestId && (completedRequests.current.has(requestId) || supersededRequests.current.has(requestId))) {
          appendProcess(requestId, item.agent_id || 'cloud_orchestrator',
            item.stage === 'narrated' ? '' : item.text || '', item.event_id, item);
        }
        return;
      }
      if (item.type !== 'agent_reply' || !item.text) return;
      if (item.scope === 'space' && item.source === 'autonomous') {
        if (item.activity_id) return; // Activity replies already appear inside their activity card.
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
  const hasDevices = agents.length > 0;
  const suggestions = hasDevices ? SUGGESTIONS : [];

  const handleSend = (text, options = {}) => {
    const t = text.trim();
    if (!t) return;
    const requestId = ++nextRequestId.current;
    setActivityLog(prev => [...prev,
      { type: 'user', text: t, requestId },
    ]);
    setTrainingSelection(null);
    send(t, {
      onSuperseded: () => supersededRequests.current.add(requestId),
      onMessage: (msg, agent, event) => appendReply({ ...event, text: msg,
        agent_id: agent || 'cloud_orchestrator' }),
      onProgress: event => {
        if (event.stage === 'finished' && !processHasContent(event)) return;
        appendProcess(requestId,
          event.agent_id || 'cloud_orchestrator',
          event.stage === 'narrated' ? '' : event.text || '', event.event_id, event);
      },
      onStatus: msg => setActivityLog(prev => insertBeforeRequestEnd(prev,requestId,
        {type:'system',requestId,text:msg,eventId:`status:${requestId}`})),
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
      selection: trainingSelection,
      intentHint: options.intentHint,
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
  let activeUid = null;
  try { activeUid = m ? decodeURIComponent(m[1]) : null; } catch (_) { /* Malformed route. */ }

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
        background: 'var(--color-bg)', color: 'var(--color-text)',
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
            {session.actor === 'admin' && <button type="button" className="icon-button device-scan-trigger"
              disabled={deviceBinding.current}
              onClick={() => setDeviceScanOpen(true)} aria-label="扫描设备" title="扫描设备">
              <Icon name="scan" size={18}/>
            </button>}
            {session.actor === 'admin' && <SpaceMenu
              onShareSpace={() => setInviteOpen(true)} onTraining={() => setTrainingOpen(true)}
              onSpaceSettings={() => setSpaceOpen(true)} onOrchestratorSettings={() => setOrchestratorOpen(true)}
              onPeople={() => setPeopleOpen(true)} onExit={logout}/>}
          </div>
        </div>

        {!hasLoadedAgents ? <div className="home-loading" aria-busy={loadingAgents}>
          <p role={agentsError ? 'alert' : 'status'}>
            {agentsError || '正在准备你的空间…'}
          </p>
          {agentsError && <button type="button" className="btn" onClick={refreshAgents} disabled={loadingAgents}>
            {loadingAgents ? '重试中…' : '重新加载'}
          </button>}
        </div> : <>
        {/* 卡片、对话和输入栏在首次设备快照就绪后一起显示。 */}
        <ChatPanel onSend={handleSend} busy={thinking}
          placeholder={loadingAgents ? '正在整理空间成员…' : '告诉我想让大家做什么…'}
          suggestions={activityLog.every(e => e.type !== 'user') && !thinking && !currentActivity ? suggestions : []}
          beforeMessages={<>
          {agentsError && (
            <div style={{ padding: '14px 16px 0' }}>
              <div className="inline-notice inline-notice--error" role="alert">
                <span>{agentsError}</span>
                <button className="btn" onClick={refreshAgents} disabled={loadingAgents}>
                  {loadingAgents ? '重试中…' : '重试'}
                </button>
              </div>
            </div>
          )}
          <DevicesScreen spaceName={session.space_name} agents={agents} unitData={unitData}
            connected={connected} recentEvents={recentEvents} loading={loadingAgents} error={agentsError}
            isAdmin={session.actor === 'admin'} onAddDevice={() => { if (!deviceBinding.current) setDeviceScanOpen(true); }}/>

          </>}
          messageContent={<ActivityFeed entries={chatRows} busy={thinking}
            training={training} agents={agents} unitData={unitData} connected={connected} activity={displayActivity}
            onLocate={location => {
              if (!location?.unit_id || !location.id) return;
              setTrainingLocation({...location,request:Date.now()});
              navigate(`#/devices/${encodeURIComponent(location.unit_id)}`);
            }}
            activityHistory={activityHistory}
            activityEvents={activityEvents} activityEnding={activityEnding}
            activityError={activityError} onEndActivity={endActivity}
            onAnswerActivity={event => {
              setActivityQuestion(event);
              navigate(`#/devices/${encodeURIComponent(event.unit_id || 'go2')}`);
            }}/>}
          >
          {trainingSelection && <div className="training-selection">修改规则：请直接输入新的要求。<button onClick={() => setTrainingSelection(null)}>取消</button></div>}
        </ChatPanel>

        </>}

        {session.actor === 'admin' && trainingOpen && <TrainingDrawer
          training={training} agents={agents}
          onClose={() => setTrainingOpen(false)}/>}
        {session.actor === 'admin' && <AdminInviteDrawer open={inviteOpen} onClose={() => setInviteOpen(false)}/>}
        {session.actor === 'admin' && <AdminSpaceDrawer open={spaceOpen} onClose={() => setSpaceOpen(false)} onInvite={() => { setSpaceOpen(false); setInviteOpen(true); }} onDevicesChanged={refreshAgents} onSpaceRenamed={() => { refreshSession().catch(() => {}); }}/>}
        {session.actor === 'admin' && <OrchestratorSettingsDrawer open={orchestratorOpen} onClose={() => setOrchestratorOpen(false)}/>}
        {session.actor === 'admin' && <SpacePeopleDrawer open={peopleOpen} onClose={() => setPeopleOpen(false)}/>}
        {session.actor === 'admin' && deviceScanOpen && <DeviceScanDialog onClose={() => setDeviceScanOpen(false)}
          onScan={handleDeviceScan} onError={message => { setDeviceScanOpen(false); showToast(message); }}/>}
        {toast && <Toast key={toast.id} notice={toast} onDismiss={dismissToast}/>}
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
            trainingLocation={trainingLocation?.unit_id === displayUid ? trainingLocation : null}
            unitId={displayUid}
            device={displayDevice}
            liveState={unitData[displayUid]}
            onBack={() => handleBack(displayUid)}
            actor={session.actor}
            sessionId={session.session_id}
            activityQuestion={activityQuestion?.unit_id === displayUid &&
              activityQuestion.activity_id === currentActivity?.activity_id ? activityQuestion : null}
          />
        </div>
      )}
    </>
  );
}
window.App = App;

function DeviceScanDialog({ onClose, onScan, onError }) {
  React.useEffect(() => {
    const onKey = event => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return <div className="device-scan-backdrop" role="presentation" onClick={onClose}>
    <section className="management-dialog device-scan-dialog" role="dialog" aria-modal="true" aria-label="扫描设备二维码" onClick={event => event.stopPropagation()}>
      <QrScanner onScan={onScan} onError={onError} frameOnly/>
    </section>
  </div>;
}
