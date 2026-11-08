/* FsmDevicePage — 通用状态机设备页。
 * 标题显示连接状态，白板统一展示状态与转移，富控件和对话按需呈现。
 * 核心控制由 Agent Card 的 transport / state_machine 驱动。
 */
function FsmDevicePage({ unitId, device, liveState, onBack, actor, activityQuestion, trainingLocation }) {
  const { useState, useEffect, useRef } = React;

  /* Card from /api/devices and SSE is authoritative. Fetch only fills a missing snapshot. */
  const [fetchedCard, setFetchedCard] = useState(null);
  const card = device?.transport || device?.state_machine ? device
    : fetchedCard?.unit_id === unitId ? fetchedCard : device;
  const sm = card?.state_machine || null;
  const transport = card?.transport || null;
  const cardLoaded = !!card;
  const [streamState, setStreamState] = useState(null);
  const [streamHealth, setStreamHealth] = useState('');
  const [eventsHealthy, setEventsHealthy] = useState(apiClient.eventsConnected !== false && apiClient.devicesSynced !== false);
  useEffect(() => {
    const opened = () => setEventsHealthy(false);
    const synced = () => setEventsHealthy(apiClient.eventsConnected !== false);
    const closed = () => setEventsHealthy(false);
    apiClient.addEventListener('events-open', opened);
    apiClient.addEventListener('events-error', closed);
    apiClient.addEventListener('devices-syncing', closed);
    apiClient.addEventListener('devices-synced', synced);
    return () => {
      apiClient.removeEventListener('events-open', opened);
      apiClient.removeEventListener('events-error', closed);
      apiClient.removeEventListener('devices-syncing', closed);
      apiClient.removeEventListener('devices-synced', synced);
    };
  }, []);
  const training = useTraining(unitId);
  const [trainingOpen, setTrainingOpen] = useState(false);
  const [trainingSelection, setTrainingSelection] = useState(null);
  const [locateTransition, setLocateTransition] = useState(null);
  useEffect(() => { if (trainingLocation?.transition_id) setLocateTransition({id:trainingLocation.transition_id,nonce:trainingLocation.nonce}); }, [trainingLocation]);
  const locate = id => setLocateTransition({id,nonce:Date.now()});
  const [controlBusy, setControlBusy] = useState(false);
  const controlPending = useRef(false);
  const snapshotCursor = useRef({ unitId, state: null, retired: new Set() });

  useEffect(() => {
    if (device?.transport || device?.state_machine) return;
    const controller = new AbortController();
    fetch('/api/devices/' + encodeURIComponent(unitId) + '/agent', { signal: controller.signal })
      .then(r => r.ok ? r.json() : null)
      .then(c => { if (!controller.signal.aborted && c?.unit_id === unitId) setFetchedCard(c); })
      .catch(() => {});
    return () => controller.abort();
  }, [unitId, device?.transport, device?.state_machine]);

  /* ── 对话 ── */
  const greeting = getAgentOpeningLine(card || unitId);
  const initialMsg = {
    id: 'welcome', role: 'assistant', agent: unitId,
    text: greeting,
  };
  const [messages, setMessages] = useState([initialMsg]);
  useEffect(() => {
    setMessages(previous => {
      if (previous[0]?.id !== 'welcome' || previous[0].text === greeting) return previous;
      return [{ ...previous[0], text: greeting }, ...previous.slice(1)];
    });
  }, [greeting]);
  const [sending, setSending] = useState(false);
  const [pendingRunId, setPendingRunId] = useState(null);
  const [execution, setExecution] = useState('');
  const seenReplies = useRef(new Set());
  const activeRun = useRef(null);
  const pendingRuns = useRef(new Map());
  const nextRequestId = useRef(0);
  const nextProcessId = useRef(0);
  const forgetRun = runId => {
    clearTimeout(pendingRuns.current.get(runId)?.timer);
    pendingRuns.current.delete(runId);
  };
  const retainRun = runId => {
    const pending = pendingRuns.current.get(runId);
    if (!pending) return;
    clearTimeout(pending.timer);
    pendingRuns.current.set(runId, { ...pending,
      timer: setTimeout(() => forgetRun(runId), 180000) });
  };
  const insertAfterRequest = (rows, requestKey, row) => {
    for (let index = rows.length - 1; index >= 0; index--) {
      if (rows[index].requestKey === requestKey) {
        return [...rows.slice(0, index + 1), row, ...rows.slice(index + 1)];
      }
    }
    return [...rows, row];
  };
  const appendProcess = (requestKey, text, eventId) => {
    if (!text) return;
    const row = { id: `${requestKey}:process:${eventId || ++nextProcessId.current}`,
      role: 'process', agent: unitId, requestKey, text, eventId };
    setMessages(previous => {
      if (eventId && previous.some(message => message.role === 'process'
        && message.requestKey === requestKey && message.eventId === eventId)) return previous;
      const endIndex = previous.findIndex(message => message.requestKey === requestKey
        && (message.terminal || message.role === 'assistant'));
      if (endIndex >= 0) return [...previous.slice(0, endIndex), row,
        ...previous.slice(endIndex)];
      return insertAfterRequest(previous, requestKey, row);
    });
  };
  const finishMessage = (requestKey, text, eventId, isError = false) => {
    if (!text) return;
    setMessages(previous => {
      const hasTerminal = previous.some(message => message.requestKey === requestKey
        && message.terminal);
      const finalBefore = previous.findIndex(message => message.role === 'assistant'
        && message.requestKey === requestKey);
      const terminal = { id: `${requestKey}:terminal`, role: 'process', agent: unitId,
        requestKey, text: '处理结束', terminal: true };
      const rows = hasTerminal ? previous : finalBefore >= 0
        ? [...previous.slice(0, finalBefore), terminal, ...previous.slice(finalBefore)]
        : insertAfterRequest(previous, requestKey, terminal);
      const finalIndex = rows.findIndex(message => message.role === 'assistant'
        && message.requestKey === requestKey);
      if (finalIndex >= 0) {
        if (isError && !rows[finalIndex].isError) return rows;
        return rows.map((message, index) => index === finalIndex
          ? { ...message, text, eventId, final: true, isError } : message);
      }
      return insertAfterRequest(rows, requestKey,
        { id: `${requestKey}:reply`, role: 'assistant', agent: unitId,
          requestKey, text, eventId, final: true, isError });
    });
  };
  useEffect(() => () => {
    pendingRuns.current.forEach(entry => clearTimeout(entry.timer));
    pendingRuns.current.clear();
  }, [unitId]);
  useEffect(() => {
    const onEvent = event => {
      const item = event.detail || {};
      if (item.agent_id !== unitId) return;
      const autonomous = item.scope === 'space' && item.source === 'autonomous';
      const pending = pendingRuns.current.get(item.run_id);
      if (!autonomous && (item.source !== 'device_chat' || !pending)) return;
      const requestKey = autonomous ? `auto:${item.run_id || item.event_id}` : pending.key;
      if (item.type === 'agent_reply' && item.text) {
        const replyKey = `${requestKey}:${item.event_id}`;
        if (seenReplies.current.has(replyKey)) return;
        seenReplies.current.add(replyKey);
        finishMessage(requestKey, item.text, item.event_id);
        if (!autonomous) {
          retainRun(item.run_id);
          if (activeRun.current === item.run_id) {
            setSending(false);
            activeRun.current = null;
          }
        }
      } else if (item.type === 'run_stage' || item.type === 'agent_progress') {
        if (item.stage !== 'finished') {
          appendProcess(requestKey, processEventText(item), item.event_id);
        }
      } else if (item.type === 'reply_error') {
        const message = '这次没能给出完整回复，请稍后再试。';
        finishMessage(requestKey, message, item.event_id, true);
        if (!autonomous) {
          retainRun(item.run_id);
          if (activeRun.current === item.run_id) {
            setSending(false);
            activeRun.current = null;
          }
        }
      }
    };
    apiClient.addEventListener('server-event', onEvent);
    return () => apiClient.removeEventListener('server-event', onEvent);
  }, [unitId]);
  const [go2AssetsOpen, setGo2AssetsOpen] = useState(false);
  const [lampAssetsOpen, setLampAssetsOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [capabilitiesOpen, setCapabilitiesOpen] = useState(false);
  const reportExecution = result => setExecution(commandBus.describe(result));

  // 命令反馈是短暂提示；设备的当前状态由下方实时状态图呈现。
  useEffect(() => {
    if (!execution || execution.startsWith('正在')) return;
    const timer = setTimeout(() => setExecution(''), 4500);
    return () => clearTimeout(timer);
  }, [execution]);

  /* Every Card replacement, including removed fields, immediately replaces the UI. */
  const modes = card?.modes || [];
  const widgets = card?.widgets || [];
  const suggestions = card?.suggestions || [];
  const [modeVals, setModeVals] = useState({});
  const modeIds = modes.map(axis => axis.id + ':' + axis.get).join(',');
  useEffect(() => {
    let active = true;
    setModeVals({});
    modes.forEach(axis => {
      if (!axis.get?.startsWith('/api/')) return;
      fetch(axis.get).then(r => r.json()).then(d => {
        if (active && d.mode) setModeVals(prev => ({ ...prev, [axis.id]: d.mode }));
      }).catch(() => {});
    });
    return () => { active = false; };
  }, [unitId, modeIds]);

  const switchMode = (axis, value) => {
    if (offline || stateHealth === 'reconnecting' || !axis.set_action && !axis.set) return;
    commandBus.command(unitId, axis.set_action || 'set_mode', { axis: axis.id, value }).then(result => {
      if (result.mode === value) {
        setModeVals(prev => ({ ...prev, [axis.id]: value }));
        setExecution('模式已更新');
      } else reportExecution(result);
    }).catch(error => reportExecution({result:'error', error:apiErrorText(error)}));
  };

  useEffect(() => {
    const url = transport?.state_stream;
    setStreamState(null);
    setStreamHealth('');
    if (!url || !url.startsWith('/api/')) return;
    let active = true;
    const es = new EventSource(url);
    es.onopen = () => { if (active) setStreamHealth('live'); };
    es.onerror = () => { if (active) setStreamHealth('reconnecting'); };
    es.onmessage = event => {
      if (!active) return;
      try {
        const state = JSON.parse(event.data);
        setStreamHealth('live');
        if (state && typeof state === 'object') setStreamState(previous => ({ unitId,
          state: mergeStateSnapshot(previous?.unitId === unitId ? previous.state : null,
            state, snapshotCursor.current.retired) }));
      } catch (_) {}
    };
    return () => { active = false; es.close(); };
  }, [unitId, transport?.state_stream]);

  const snapshot = liveState?.state || card?.state || {};
  if (snapshotCursor.current.unitId !== unitId) {
    snapshotCursor.current = { unitId, state: null, retired: new Set() };
  }
  const cursor = snapshotCursor.current;
  cursor.state = mergeStateSnapshot(cursor.state, snapshot, cursor.retired);
  cursor.state = mergeStateSnapshot(cursor.state, streamState?.unitId === unitId ? streamState.state : null, cursor.retired);
  const runtimeState = cursor.state || {};
  const regions = stateMachineRegions(sm);
  const primaryRegion = primaryStateRegion(sm);
  const stateHealth = transport?.state_stream ? streamHealth : eventsHealthy ? 'live' : 'reconnecting';
  const stateField = sm?.state_field || (!card?.card_schema_version
    ? ('fsm_state' in runtimeState ? 'fsm_state' : 'ism' in runtimeState ? 'ism' : '') : '');
  const connectionField = sm?.connection_field || (!card?.card_schema_version && 'connected' in runtimeState ? 'connected' : '');
  const current = runtimeState[primaryRegion?.state_field || stateField] || '';
  const connected = connectionField ? runtimeState[connectionField] === true : !!(card?._online ?? card?.online);
  const offline = !(card?._online ?? card?.online) || (connectionField && !connected);
  const meta = getAgentMeta(card || { unit_id: unitId, name: unitId });
  const ACCENT = meta.color;
  const hasUserMsg = messages.some(m => m.role === 'user');
  const statusLabel = runtimeState.connection_state === 'connecting' ? '连接中'
    : offline ? '未连接' : stateHealth === 'reconnecting' ? '状态同步中' : '在线';
  const safetyTransitions = regions.flatMap(region => (region.transitions || [])
    .filter(t => t.safety && t.action && t.src === runtimeState[region.state_field]));
  // Missing posture feedback must never remove the independent stop control.
  const safetyAction = safetyTransitions[0] || (connected
    ? regions.flatMap(region => region.transitions || []).find(t => t.safety && t.action) : null);
  const effectiveModeVals = { ...modeVals };
  modes.forEach(axis => {
    if (axis.state_field && runtimeState[axis.state_field] != null) effectiveModeVals[axis.id] = runtimeState[axis.state_field];
  });

  /* ── 派发 FSM 转移 ── */
  const dispatchTransition = async t => {
    if (offline) { setExecution('设备未连接，暂不能执行动作'); return; }
    if (!t?.safety && stateHealth === 'reconnecting') {
      setExecution('状态连接正在恢复，等待最新状态'); return;
    }
    if (!t?.safety && controlPending.current) {
      setExecution('正在等待本次执行结果，请勿重复操作'); return;
    }
    if (!t?.action) {
      setExecution(t?.hint || '当前状态不允许这个动作');
      return;
    }
    if (!t.safety) { controlPending.current = true; setControlBusy(true); }
    setExecution(t.prepare_action ? '正在先起身，确认站稳后执行目标动作…'
      : t.dispatch_only ? '正在发送指令…' : '正在等待设备执行反馈…');
    try {
      const params = t.id ? Object.fromEntries((t.training_fixed_params || []).filter(key => Object.hasOwn(t.params || {}, key)).map(key => [key, t.params[key]])) : (t.params || {});
      const result = await commandBus.command(unitId, t.prepare_action || t.action,
        t.prepare_action ? {cmd:t.action,prepare_standing:true} : params);
      reportExecution(result);
    } catch (error) {
      reportExecution({ result: 'error', error: apiErrorText(error, '这次请求没有完成，请检查设备状态') });
    } finally {
      if (!t.safety) { controlPending.current = false; setControlBusy(false); }
    }
  };

  /* ── 发送对话 ── */
  const handleSend = text => {
    if (!text || sending) return;
    const runId = pendingRunId || (unitId === 'go2' ? 'go2_' : 'lamp_') +
      crypto.randomUUID().replaceAll('-', '');
    if (pendingRuns.current.has(runId)) forgetRun(runId);
    const requestKey = `chat:${++nextRequestId.current}`;
    setMessages(prev => [...prev, { id: `${requestKey}:user`, role: 'user', text },
      { id: `${requestKey}:initial`, role: 'process', agent: unitId, requestKey,
        text: '已收到请求', eventId: `initial:${requestKey}` }]);
    setSending(true);
    activeRun.current = runId;
    pendingRuns.current.set(runId, { key: requestKey, timer: null });
    const answerText = activityQuestion && unitId === 'go2'
      ? `回答你刚才的问题「${activityQuestion.text}」（${activityQuestion.question_id || activityQuestion.event_id}）：${text}`
      : text;
    const params = { message: answerText, ...(trainingSelection ? {selection:trainingSelection} : {}), ...(unitId === 'go2' ? { task_id: runId } : { run_id: runId }) };
    if (pendingRunId) {
      if (unitId === 'go2') params.resume_run_id = pendingRunId;
      else { params.run_id = pendingRunId; params.resume = true; }
    }
    setTrainingSelection(null);
    commandBus.command(unitId, 'chat', params)
      .then(result => {
        setPendingRunId(result?.result === 'needs_clarification' ? (result.run_id || result.task_id) : null);
        if (!pendingRuns.current.has(runId)) return;
        const reply = result?.reply || result?.response;
        const replyKey = `${requestKey}:${result?.event_id}`;
        if (result?.event_id && reply && !seenReplies.current.has(replyKey)) {
          seenReplies.current.add(replyKey);
          finishMessage(requestKey, reply, result.event_id);
          setSending(false);
          activeRun.current = null;
          retainRun(runId);
        } else if (!result?.event_id || !reply) {
          const fallback = reply || result?.question || apiErrorText(
            { message: commandBus.describe(result) }, '设备暂时无法完成处理，请稍后重试。');
          if (['accepted', 'executing', 'timeout'].includes(result?.result)) {
            appendProcess(requestKey, fallback, `pending:${result?.result}`);
          } else {
            finishMessage(requestKey, fallback, result?.event_id,
              !reply && !result?.question);
          }
          setSending(false);
          activeRun.current = null;
          // HTTP/query can finish before the matching SSE event. Keep its ID
          // briefly so a late reply or error still attaches to this task.
          retainRun(runId);
        }
      })
      .catch(error => {
        if (!pendingRuns.current.has(runId)) return;
        finishMessage(requestKey, apiErrorText(error), undefined, true);
        if (activeRun.current === runId) {
          setSending(false);
          activeRun.current = null;
        }
        retainRun(runId);
      });
  };

  const isGo2 = unitId === 'go2' || card?.device_id === 'go2'; // dedicated asset settings only
  const isLamp = (card?.skills || []).some(skill => skill.id === 'adjust_lighting');

  return (
    <div className="device-page" style={{
      position: 'fixed', inset: 0,
      background: 'var(--color-bg)', color: '#ccc',
      fontFamily: 'var(--font-sans)',
      paddingTop: 'env(safe-area-inset-top,0px)',
      display: 'flex', flexDirection: 'column',
    }}>

      {/* ── 头部 ── */}
      <div className="device-page__header" style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '7px 16px',
        borderBottom: '1px solid var(--color-border)',
        background: 'var(--color-surface-1)',
        flexShrink: 0,
      }}>
        {/* 返回按钮 */}
        <button onClick={onBack} aria-label="返回空间" style={{
          background: 'none', border: 'none',
          color: 'var(--color-text-dim)', cursor: 'pointer',
          fontSize: 18, padding: '0 4px', lineHeight: 1,
          fontFamily: 'inherit', WebkitTapHighlightColor: 'transparent',
        }}>←</button>

        {/* 设备名与右侧实时状态 */}
        <div style={{ minWidth: 0, flex: '0 1 auto' }}>
          <div style={{
            fontSize: 15, fontWeight: 700, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            color: offline ? 'var(--color-text-dim)' : ACCENT,
          }}>{getAgentDisplayName(card || unitId)}</div>
        </div>
        <div className="device-header__status" title={statusLabel}>
          <i className={offline ? 'is-offline' : ''}/>
          <span>{statusLabel}</span>
        </div>
        {!isGo2 && safetyAction && <button type="button" className="device-header__stop btn btn--danger"
          disabled={offline || !connected} onClick={() => dispatchTransition(safetyAction)}>{safetyAction.label}</button>}
        <button type="button" className="icon-button device-header__settings"
          onClick={() => setSettingsOpen(true)} aria-label="管理" title="管理"><Icon name="settings" size={16}/></button>
      </div>

      {!cardLoaded && execution && <div role="status" aria-live="polite" style={{ padding: '8px 16px', fontSize: 12 }}>{execution}</div>}
      {/* ── Card 驱动的设备白板。 ── */}
      {cardLoaded && (
        <div className="device-state-workspace device-state-workspace--shared">
          <DeviceStateBoard key={unitId} unitId={unitId} sm={sm} current={current} card={card}
            widgets={widgets} liveState={runtimeState} connected={connected} offline={offline}
            actor={actor} color={ACCENT} busy={controlBusy} streamHealth={stateHealth} feedback={execution}
            onTransition={dispatchTransition} report={reportExecution} training={training} locateTransition={locateTransition}
            onTeach={(transitionId,record) => { setTrainingSelection({unit_id:unitId,transition_id:transitionId,...(record ? {teaching_id:record.teaching_id} : {})}); setTrainingOpen(false); }} />
        </div>
      )}

      {/* ── ChatPanel（撑满剩余高度）；离线整体置灰并禁用输入 ── */}
      <div className="device-page__chat" style={{
        flex: '1 1 180px', minHeight: 150, display: 'flex', flexDirection: 'column',
        filter: offline ? 'grayscale(1)' : 'none',
        opacity: offline ? 0.45 : 1,
        transition: 'filter 0.2s, opacity 0.2s',
      }}>
      {trainingSelection && <div className="training-selection" role="status">正在教导所选转换，请在下方输入你的要求。<button onClick={() => setTrainingSelection(null)}>取消</button></div>}
      <TrainingPreview proposal={training.proposal?.unit_id === unitId ? training.proposal : null} error={training.error} onConfirm={async () => { const id = training.proposal?.transition_ids?.[0]; if (await training.confirm()) locate(id); }} onCancel={training.cancel} onLocate={(_,id) => locate(id)}/>
      {activityQuestion && <div className="device-activity-question">
        <strong>{getAgentDisplayName(unitId)}刚才问：</strong>
        <span>{activityQuestion.text}</span>
        {activityMediaUrl(activityQuestion) && <a href={activityMediaUrl(activityQuestion)}
          target="_blank" rel="noopener noreferrer">查看截图</a>}
      </div>}
      <ChatPanel
        messages={messages}
        thinking={false}
        thinkingText=""
        thinkingAgent={unitId}
        deviceName={getAgentDisplayName(card || unitId)}
        onSend={handleSend}
        placeholder={offline ? '设备离线，暂不可用' : sending ? '正在处理…' : '告诉设备要做什么…'}
        disabled={offline || sending}
        variant="inline"
      >
        {/* 快捷指令（card.suggestions 驱动）：无用户消息时显示，位置与主屏建议一致 */}
        {suggestions.length > 0 && !hasUserMsg && !sending && !offline && (
          <div style={{
            padding: '6px 12px 0', display: 'flex', gap: 6,
            overflowX: 'auto', scrollbarWidth: 'none',
          }}>
            {suggestions.map(cmd => (
              <button key={cmd} onClick={() => handleSend(cmd)} style={{
                flexShrink: 0, padding: '7px 14px',
                borderRadius: 'var(--radius-card)',
                background: 'var(--color-surface-2)',
                border: '1px solid var(--color-border-strong)',
                color: 'var(--color-text-muted)', fontSize: 13,
                cursor: 'pointer', fontFamily: 'inherit', whiteSpace: 'nowrap',
                WebkitTapHighlightColor: 'transparent',
              }}>{cmd}</button>
            ))}
          </div>
        )}
      </ChatPanel>
      </div>
      {trainingOpen && <TrainingDrawer unitId={unitId} training={training} onClose={() => setTrainingOpen(false)} onLocate={(_,id) => {locate(id); setTrainingOpen(false);}} onRevise={record => {setTrainingSelection({unit_id:unitId,transition_id:record.transition_ids[0],teaching_id:record.teaching_id}); setTrainingOpen(false);}}/>}
      <DeviceSettingsMenu open={settingsOpen} onClose={() => setSettingsOpen(false)}
        card={card}
        modes={modes} modeVals={effectiveModeVals} onSwitchMode={switchMode}
        offline={offline} connected={connected} safetyAction={safetyAction}
        onSafety={() => { setSettingsOpen(false); dispatchTransition(safetyAction); }}
        isGo2={isGo2} isLamp={isLamp} actor={actor} unitId={unitId}
        onGo2Settings={() => setGo2AssetsOpen(true)}
        onTraining={() => setTrainingOpen(true)}
        onCapabilities={() => setCapabilitiesOpen(true)}
        onLampSettings={() => setLampAssetsOpen(true)}/>
      <DeviceCapabilitiesDrawer open={capabilitiesOpen} onClose={() => setCapabilitiesOpen(false)} card={card}/>
      {actor === 'admin' && isGo2 && <Go2AssetsDrawer open={go2AssetsOpen} onClose={() => setGo2AssetsOpen(false)}/>}
      {actor === 'admin' && isLamp && <LampAssetsDrawer unitId={unitId} open={lampAssetsOpen}
        onClose={() => setLampAssetsOpen(false)}/>}
    </div>
  );
}
window.FsmDevicePage = FsmDevicePage;

function LampAssetsDrawer({ unitId, open, onClose }) {
  const { useEffect, useState } = React;
  const [memory, setMemory] = useState({ episodes: [], daily_summary: '' });
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState('');
  const base = '/api/esp32/' + encodeURIComponent(unitId);
  const load = async () => {
    setBusy(true);
    try {
      const records = await apiClient.request(base + '/memory');
      setMemory(records || { episodes: [], daily_summary: '' });
      setFeedback('');
    } catch (error) { setFeedback(apiErrorText(error, '灯的记忆暂时无法读取')); }
    finally { setBusy(false); }
  };
  useEffect(() => { if (open) load(); }, [open, unitId]);
  useEffect(() => {
    if (!open) return undefined;
    const onKey = event => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);
  return <>
    <div className={`drawer-mask ${open ? 'is-open' : ''}`} onClick={onClose}/>
    <aside className={`management-dialog space-drawer go2-assets-drawer ${open ? 'is-open' : ''}`}
      role="dialog" aria-modal="true" aria-hidden={!open} inert={open ? undefined : ''}
      style={{ visibility: open ? 'visible' : 'hidden' }} aria-labelledby="lamp-assets-title">
      <header className="space-drawer__header">
        <div><h2 id="lamp-assets-title">{getAgentDisplayName(unitId)}管理</h2></div>
        <div className="space-drawer__header-actions">
          <button className="btn" onClick={load} disabled={busy}>刷新</button>
          <button className="icon-button space-drawer__close" onClick={onClose} aria-label="关闭灯管理"><Icon name="x" size={16}/></button>
        </div>
      </header>
      <div className="space-drawer__body" aria-busy={busy}>
        {feedback && <p role="alert" className="action-feedback action-feedback--error">{feedback}</p>}
        <section className="asset-section">
          <div className="asset-section__heading"><div><h3>灯的人格与特质</h3></div><span>长期生效</span></div>
          <AgentPersonalitySettings agentId={unitId} active={open}/>
        </section>
        <section className="asset-section asset-section--memory">
          <div className="asset-section__heading"><div><h3>灯的记忆</h3></div><span>经历与摘要</span></div>
          <div className="memory-block"><h4>今日记录</h4><pre>{memory.daily_summary || '（今天暂无记录）'}</pre></div>
          <div className="memory-block"><h4>近期经历</h4>
            {memory.episodes?.length ? <ul>{memory.episodes.map((entry, index) =>
              <li key={entry.ts || index}><span>{entry.detail}</span><code>{entry.result}</code></li>)}</ul>
              : <p className="asset-empty">还没有保存的经历。</p>}
          </div>
        </section>
      </div>
    </aside>
  </>;
}

function FsmBoard(props) {
  return <DeviceStateBoard {...props} offline={!props.connected}
    onTransition={transition => props.onFire?.(transition.trigger)}/>;
}

function DeviceManagementPanel({ open, onClose, title, children }) {
  const panelRef = React.useRef(null);
  const closeRef = React.useRef(onClose);
  closeRef.current = onClose;
  React.useEffect(() => {
    if (!open) return undefined;
    panelRef.current?.querySelector('button')?.focus();
    const onKey = event => {
      if (event.key === 'Escape') closeRef.current();
      if (event.key !== 'Tab') return;
      const focusables = panelRef.current?.querySelectorAll('button:not([disabled]), a[href]');
      if (!focusables?.length) return;
      if (event.shiftKey && document.activeElement === focusables[0]) {
        event.preventDefault();
        focusables[focusables.length - 1].focus();
      } else if (!event.shiftKey && document.activeElement === focusables[focusables.length - 1]) {
        event.preventDefault();
        focusables[0].focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);
  if (!open) return null;

  return <div className="device-settings-layer">
    <div className="device-settings-scrim" onClick={onClose}/>
    <section ref={panelRef} className="management-dialog device-settings-panel" role="dialog" aria-modal="true" aria-label={title}>
      <header><h2>{title}</h2><button type="button" className="icon-button" onClick={onClose} aria-label={`关闭${title}`}><Icon name="x" size={16}/></button></header>
      <div className="device-settings-body">{children}</div>
    </section>
  </div>;
}

function DeviceSettingsMenu({ open, onClose, modes, modeVals, onSwitchMode, offline, connected, safetyAction, onSafety, isGo2, isLamp, actor, unitId, onGo2Settings, onLampSettings, onTraining, onCapabilities }) {
  return <DeviceManagementPanel open={open} onClose={onClose} title="管理">
      {safetyAction && <button type="button" className="btn btn--danger device-settings-safety"
        onClick={onSafety} disabled={offline || !connected}>{safetyAction.label}</button>}
      {actor === 'admin' && modes.map(axis => <section key={axis.id} className="device-settings-axis">
        <h3>{axis.label}</h3>
        <div>{axis.options.map(option => <button type="button" key={option.value}
          className={modeVals[axis.id] === option.value ? 'is-active' : ''}
          onClick={() => onSwitchMode(axis, option.value)} disabled={offline}
          title={option.description}>
          <strong>{option.label}</strong><small>{option.description}</small>
        </button>)}</div>
      </section>)}
      {actor === 'admin' && isGo2 && <button type="button" className="device-settings-go2" onClick={() => { onClose(); onGo2Settings(); }}>
        <span><strong>Go2 管理</strong><small>地点、人设与记忆</small></span><Icon name="arrow" size={15}/>
      </button>}
      {actor === 'admin' && isLamp && <button type="button" className="device-settings-go2" onClick={() => { onClose(); onLampSettings(); }}>
        <span><strong>灯智能体管理</strong><small>人设与记忆</small></span><Icon name="arrow" size={15}/>
      </button>}
      {actor === 'admin' && <button type="button" className="device-settings-go2" onClick={() => { onClose(); onTraining(); }}>
        <span><strong>伙伴训练</strong><small>查看、调整或暂停已保存的训练</small></span><Icon name="arrow" size={15}/>
      </button>}
      <button type="button" className="device-settings-go2" onClick={() => { onClose(); onCapabilities(); }}>
        <span><strong>设备能力与技能</strong><small>查看观察条件与公开技能</small></span><Icon name="arrow" size={15}/>
      </button>
      <a className="device-settings-card" href={'/api/devices/' + encodeURIComponent(unitId) + '/agent'}
        target="_blank" rel="noopener noreferrer">
        <span><strong>Agent Card</strong><small>查看设备能力、状态机和控件声明</small></span><Icon name="arrow" size={15}/>
      </a>
  </DeviceManagementPanel>;
}

function DeviceCapabilitiesDrawer({ open, onClose, card }) {
  return <DeviceManagementPanel open={open} onClose={onClose} title="设备能力与技能">
      <div className="device-settings-capabilities">
        <section><h3>可用于自动触发的观察</h3>
          {card?.observations?.length ? card.observations.map(obs =>
            <p key={obs.id}><strong>{obs.name || obs.id}</strong><small>{obs.available === false ? '当前不可用 · ' : ''}{obs.description || '设备声明的观察条件'}</small></p>)
            : <p><small>该设备暂未声明自动触发条件。</small></p>}
        </section>
        <section><h3>公开技能</h3>
          {card?.skills?.length ? card.skills.map(skill =>
            <p key={skill.id}><strong>{skill.name}</strong>
              <small>{skill.tags?.includes('persistent') ? '需管理员操作' : '可手动或通过对话调用'} · {skill.description}</small></p>)
            : <p><small>该设备暂未声明公开技能。</small></p>}
        </section>
      </div>
  </DeviceManagementPanel>;
}
