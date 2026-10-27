/* FsmDevicePage — 通用状态机设备页。
 * 固定控制区展示 Card 声明的状态与转移；当前态内容和对话排在其后。
 * 核心控制由 Agent Card 的 transport / state_machine 驱动。
 */
function FsmDevicePage({ unitId, device, liveState, onBack, actor, activityQuestion }) {
  const { useState, useEffect, useRef } = React;

  /* Card from /api/devices and SSE is authoritative. Fetch only fills a missing snapshot. */
  const [fetchedCard, setFetchedCard] = useState(null);
  const card = device?.transport || device?.state_machine ? device
    : fetchedCard?.unit_id === unitId ? fetchedCard : device;
  const sm = card?.state_machine || null;
  const transport = card?.transport || null;
  const cardLoaded = !!card;
  const [streamState, setStreamState] = useState(null);
  const [previewState, setPreviewState] = useState(null);

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
  const initialMsg = {
    role: 'assistant', agent: unitId,
    text: `你好，我是 ${card?.name || getAgentDisplayName(unitId)}，有什么可以帮你？`,
  };
  const [messages, setMessages] = useState([initialMsg]);
  const [sending, setSending] = useState(false);
  const [pendingRunId, setPendingRunId] = useState(null);
  const [execution, setExecution] = useState('');
  const seenReplies = useRef(new Set());
  const seenProgress = useRef(new Set());
  const activeRun = useRef(null);
  const pendingRuns = useRef(new Map());
  const nextRequestId = useRef(0);
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
  const finishMessage = (requestKey, text, eventId, isError = false) => {
    if (!text) return;
    setMessages(previous => {
      const status = previous.map(message => message.role === 'status'
        && message.requestKey === requestKey
        ? { ...message, text: '处理结束', final: true } : message);
      const finalIndex = status.findIndex(message => message.role === 'assistant'
        && message.requestKey === requestKey);
      if (finalIndex >= 0) {
        if (isError && !status[finalIndex].isError) return status;
        return status.map((message, index) => index === finalIndex
          ? { ...message, text, eventId, final: true, isError } : message);
      }
      return insertAfterRequest(status, requestKey,
        { id: `${requestKey}:reply`, role: 'assistant', agent: unitId,
          requestKey, text, eventId, final: true, isError });
    });
  };
  const updateStatus = (requestKey, text) => {
    if (!text) return;
    setMessages(previous => {
      const status = previous.find(message => message.role === 'status'
        && message.requestKey === requestKey);
      if (status?.final) return previous;
      if (status) return previous.map(message => message === status
        ? { ...message, text } : message);
      const finalIndex = previous.findIndex(message => message.role === 'assistant'
        && message.requestKey === requestKey);
      const row = { id: `${requestKey}:status`, role: 'status', agent: unitId,
        requestKey, text: finalIndex < 0 ? text : '处理结束', final: finalIndex >= 0 };
      if (finalIndex < 0) return insertAfterRequest(previous, requestKey, row);
      return [...previous.slice(0, finalIndex), row, ...previous.slice(finalIndex)];
    });
  };
  const updateStep = (requestKey, item) => {
    const stepId = item.step_id || item.event_id;
    if (!stepId) return;
    setMessages(previous => {
      const index = previous.findIndex(message => message.role === 'step'
        && message.requestKey === requestKey && message.stepId === stepId);
      if (index >= 0) return previous.map((message, at) => at === index
        ? mergeRunStep(message, item) : message);
      const row = { id: `${requestKey}:step:${stepId}`, role: 'step', agent: unitId,
        requestKey, stepId, event: item,
        narrative: item.stage === 'narrated' ? item.text : '' };
      const finalIndex = previous.findIndex(message => message.role === 'assistant'
        && message.requestKey === requestKey);
      if (finalIndex < 0) return insertAfterRequest(previous, requestKey, row);
      return [...previous.slice(0, finalIndex), row, ...previous.slice(finalIndex)];
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
        const progressKey = `${requestKey}:${item.event_id}`;
        if (seenProgress.current.has(progressKey)) return;
        seenProgress.current.add(progressKey);
        if (item.step_id) updateStep(requestKey, item);
        else updateStatus(requestKey, describeAgentStatus(item) || '正在处理');
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
    if (offline || !axis.set_action && !axis.set) return;
    commandBus.command(unitId, axis.set_action || 'set_mode', { axis: axis.id, value }).then(result => {
      if (result.mode === value) {
        setModeVals(prev => ({ ...prev, [axis.id]: value }));
        setExecution('模式已更新');
      } else reportExecution(result);
    });
  };

  useEffect(() => {
    const url = transport?.state_stream;
    setStreamState(null);
    if (!url || !url.startsWith('/api/')) return;
    const es = new EventSource(url);
    es.onmessage = event => {
      try {
        const state = JSON.parse(event.data);
        if (state && typeof state === 'object') setStreamState(state);
      } catch (_) {}
    };
    return () => es.close();
  }, [unitId, transport?.state_stream]);

  const snapshot = liveState?.state || card?.state || {};
  const runtimeState = streamState ? { ...snapshot, ...streamState } : snapshot;
  const stateField = sm?.state_field || (!card?.card_schema_version
    ? ('fsm_state' in runtimeState ? 'fsm_state' : 'ism' in runtimeState ? 'ism' : '') : '');
  const connectionField = sm?.connection_field || (!card?.card_schema_version && 'connected' in runtimeState ? 'connected' : '');
  const current = stateField ? runtimeState[stateField] || '' : '';
  const connected = connectionField ? runtimeState[connectionField] === true : !!(card?._online ?? card?.online);
  const offline = !(card?._online ?? card?.online) || (connectionField && !connected);
  const meta = getAgentMeta(card || { unit_id: unitId, name: unitId });
  const ACCENT = meta.color;
  const hasUserMsg = messages.some(m => m.role === 'user');
  const visibleWidgets = widgets.filter(w => w.visible !== false
    && (!w.states?.length || w.states.includes(current))
    && (!w.requires_connection || connected)
    && (!w.preview_only || previewState === current));
  const hasBoardWidget = visibleWidgets.some(widget => widget.type !== 'video' && widget.type !== 'map');
  const statusLabel = offline ? '离线' : `在线 · ${sm?.views?.[current]?.label || '状态待同步'}`;
  const hasActionPreview = (sm?.states || []).some(state => {
    const type = sm?.views?.[state]?.type;
    return type === 'robot_pose' || type === 'image';
  });
  const safetyTransitions = (sm?.transitions || []).filter(t => t.safety && t.action);
  const safetyAction = safetyTransitions.find(t => t.src === current) || safetyTransitions[0];

  /* ── 派发 FSM 转移 ── */
  const dispatchTransition = async t => {
    if (offline) return;
    if (!t?.action) {
      setExecution(t?.hint || '当前状态不允许这个动作');
      return;
    }
    setExecution('正在等待设备执行反馈…');
    const result = await commandBus.command(unitId, t.action, t.params || {});
    if (t.safety && result?.ok) {
      setExecution(`${t.label}已发送，等待状态同步`);
    } else if (result?.result === 'accepted' && result.fsm_state === t.dst) {
      setExecution('');
    } else {
      reportExecution(result);
    }
  };
  const fire = trigger => dispatchTransition((sm?.transitions || []).find(
    t => t.src === current && t.trigger === trigger
  ));

  /* ── 发送对话 ── */
  const handleSend = text => {
    if (!text || sending) return;
    const runId = pendingRunId || (unitId === 'go2' ? 'go2_' : 'lamp_') +
      crypto.randomUUID().replaceAll('-', '');
    if (pendingRuns.current.has(runId)) forgetRun(runId);
    const requestKey = `chat:${++nextRequestId.current}`;
    setMessages(prev => [...prev, { id: `${requestKey}:user`, role: 'user', text },
      { id: `${requestKey}:status`, role: 'status', agent: unitId, requestKey,
        text: '正在了解你的需求' }]);
    setSending(true);
    activeRun.current = runId;
    pendingRuns.current.set(runId, { key: requestKey, timer: null });
    const answerText = activityQuestion && unitId === 'go2'
      ? `回答你刚才的问题「${activityQuestion.text}」（${activityQuestion.question_id || activityQuestion.event_id}）：${text}`
      : text;
    const params = { message: answerText, ...(unitId === 'go2' ? { task_id: runId } : { run_id: runId }) };
    if (pendingRunId) {
      if (unitId === 'go2') params.resume_run_id = pendingRunId;
      else { params.run_id = pendingRunId; params.resume = true; }
    }
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
            updateStatus(requestKey, fallback);
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
  useEffect(() => { setPreviewState(null); }, [unitId]);
  const isOn = !!current && current !== sm?.initial;

  return (
    <div style={{
      position: 'fixed', inset: 0,
      background: 'var(--color-bg)', color: '#ccc',
      fontFamily: 'var(--font-sans)',
      paddingTop: 'env(safe-area-inset-top,0px)',
      display: 'flex', flexDirection: 'column',
    }}>

      {/* ── 头部 ── */}
      <div style={{
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
            textShadow: (isOn && !offline) ? `0 0 12px ${ACCENT}80` : 'none',
            transition: 'text-shadow 0.4s',
          }}>{card?.name || getAgentDisplayName(unitId)}</div>
        </div>
        <div className="device-header__status" title={statusLabel}>
          <i className={offline ? 'is-offline' : ''}/>
          <span>{statusLabel}</span>
        </div>
        <button type="button" className="device-header__settings"
          onClick={() => setSettingsOpen(true)} aria-label="设备设置">⋯</button>
      </div>

      {execution && <div role="status" aria-live="polite" style={{ padding: '8px 16px', fontSize: 12 }}>{execution}</div>}
      {/* ── Card 驱动的设备白板。 ── */}
      {cardLoaded && (
        <div className={`device-state-workspace${hasBoardWidget ? ' device-state-workspace--expanded' : ''}`} style={{
          filter: offline ? 'grayscale(1)' : 'none',
          opacity: offline ? 0.45 : 1,
          pointerEvents: offline ? 'none' : 'auto',
          transition: 'filter 0.2s, opacity 0.2s',
        }}>
          <FsmBoard unitId={unitId} sm={sm} current={current} widgets={widgets}
            onFire={fire} report={reportExecution} actor={actor} color={ACCENT}
            liveState={runtimeState}
            stateField={stateField} connectionField={connectionField}
            connected={connected} selectedState={previewState}
            onSelectState={hasActionPreview
              ? state => setPreviewState(previous => previous === state ? null : state)
              : null}
            legacyLed={sm?.state_field === 'ism' && sm?.states?.includes('BRIGHT')}/>
        </div>
      )}

      {/* ── ChatPanel（撑满剩余高度）；离线整体置灰并禁用输入 ── */}
      <div style={{
        flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column',
        filter: offline ? 'grayscale(1)' : 'none',
        opacity: offline ? 0.45 : 1,
        transition: 'filter 0.2s, opacity 0.2s',
      }}>
      <div className="device-chat-heading">与{card?.name || getAgentDisplayName(unitId)}对话</div>
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
        deviceName={card?.name}
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
      <DeviceSettingsMenu open={settingsOpen} onClose={() => setSettingsOpen(false)}
        modes={modes} modeVals={modeVals} onSwitchMode={switchMode}
        offline={offline} connected={connected} safetyAction={safetyAction}
        onSafety={() => { setSettingsOpen(false); dispatchTransition(safetyAction); }}
        isGo2={isGo2} isLamp={isLamp} actor={actor} unitId={unitId}
        onGo2Settings={() => setGo2AssetsOpen(true)}
        onLampSettings={() => setLampAssetsOpen(true)}/>
      {actor === 'admin' && isGo2 && <Go2AssetsDrawer open={go2AssetsOpen} onClose={() => setGo2AssetsOpen(false)}/>}
      {actor === 'admin' && isLamp && <LampAssetsDrawer unitId={unitId} open={lampAssetsOpen}
        onClose={() => setLampAssetsOpen(false)}/>}
    </div>
  );
}
window.FsmDevicePage = FsmDevicePage;

function LampAssetsDrawer({ unitId, open, onClose }) {
  const { useEffect, useState } = React;
  const [personality, setPersonality] = useState('');
  const [socialInitiative, setSocialInitiative] = useState(70);
  const [memory, setMemory] = useState({ taught: [], episodes: [], daily_summary: '' });
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState('');
  const base = '/api/esp32/' + encodeURIComponent(unitId);
  const load = async () => {
    setBusy(true);
    try {
      const [profile, records] = await Promise.all([
        apiClient.request(base + '/personality'), apiClient.request(base + '/memory'),
      ]);
      setPersonality(profile.prompt || '');
      setSocialInitiative(profile.social_initiative ?? 70);
      setMemory(records || { taught: [], episodes: [], daily_summary: '' });
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
  const save = async event => {
    event.preventDefault();
    if (!personality.trim() || busy) return;
    setBusy(true);
    try {
      await apiClient.request(base + '/personality', { method: 'POST',
        body: { prompt: personality.trim(), social_initiative: Number(socialInitiative) } });
      setFeedback('人设已更新');
    } catch (error) { setFeedback(apiErrorText(error, '人设更新失败')); }
    finally { setBusy(false); }
  };
  return <>
    <div className={`drawer-mask ${open ? 'is-open' : ''}`} onClick={onClose}/>
    <aside className={`space-drawer go2-assets-drawer ${open ? 'is-open' : ''}`}
      role="dialog" aria-modal="true" aria-hidden={!open} aria-labelledby="lamp-assets-title">
      <div className="space-drawer__handle"/>
      <header className="space-drawer__header">
        <div><small>LAMP AGENT</small><h2 id="lamp-assets-title">灯智能体设置</h2></div>
        <div className="space-drawer__header-actions">
          <button className="btn" onClick={load} disabled={busy}>刷新</button>
          <button className="btn space-drawer__close" onClick={onClose} aria-label="关闭灯设置"><Icon name="x" size={14}/></button>
        </div>
      </header>
      <div className="space-drawer__body" aria-busy={busy}>
        {feedback && <p role="status" className="action-feedback">{feedback}</p>}
        <section className="asset-section">
          <div className="asset-section__heading"><div><small>SOUL PROFILE</small><h3>灯的人设</h3></div><span>长期生效</span></div>
          <form className="personality-editor" onSubmit={save}>
            <textarea value={personality} onChange={event => setPersonality(event.target.value)}
              rows="5" maxLength="2000" placeholder="描述灯的表达方式…"/>
            <label>社交主动性 {socialInitiative} · {socialInitiative < 35 ? '低' : socialInitiative < 70 ? '中' : '高'}
              <input type="range" min="0" max="100" value={socialInitiative}
                onChange={event => setSocialInitiative(Number(event.target.value))}/>
            </label>
            <div><small>{personality.length} / 2000</small><button className="btn btn--accent" disabled={busy || !personality.trim()}>更新人设</button></div>
          </form>
        </section>
        <section className="asset-section asset-section--memory">
          <div className="asset-section__heading"><div><small>LONG MEMORY</small><h3>灯的记忆</h3></div><span>{memory.taught?.length || 0} 条调教</span></div>
          <div className="memory-block"><h4>今日记录</h4><pre>{memory.daily_summary || '（今天暂无记录）'}</pre></div>
          <div className="memory-block"><h4>近期经历</h4>
            {memory.episodes?.length ? <ul>{memory.episodes.map((entry, index) =>
              <li key={entry.ts || index}><span>{entry.detail}</span><code>{entry.result}</code></li>)}</ul>
              : <p className="asset-empty">还没有保存的经历。</p>}
          </div>
          <div className="memory-block"><h4>已教习惯</h4>
            {memory.taught?.length ? <ul>{memory.taught.map((rule, index) =>
              <li key={rule.id || index}><span>{rule.trigger} → {rule.behavior}</span><code>{rule.hit_count || 0} 次触发</code></li>)}</ul>
              : <p className="asset-empty">还没有保存的调教习惯。</p>}
          </div>
        </section>
      </div>
    </aside>
  </>;
}

function FsmBoard({ unitId, sm, current, widgets, onFire, report, actor, color, liveState, stateField, connectionField, legacyLed, connected, selectedState, onSelectState }) {
  const { useState, useRef, useEffect } = React;
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [zoom, setZoom] = useState(1);
  const drag = useRef(null);
  const pointers = useRef(new Map());
  const pinch = useRef(null);
  const activeWidgets = widgets.map((widget, index) => ({ ...widget, _index: index }))
    .filter(widget => !widget.states?.length || widget.states.includes(current));
  const visibleWidgets = activeWidgets.filter(widget => widget.visible !== false)
    .sort((a, b) => (a.order ?? a._index) - (b.order ?? b._index));
  const placement = widget => widget.placement === 'media' || (!widget.placement && widget.type === 'video')
    ? 'media' : 'controls';
  const displayed = visibleWidgets.filter(widget => (!widget.requires_connection || connected)
    && (!widget.preview_only || selectedState === current));
  const videoWidgets = displayed.filter(widget => widget.type === 'video');
  const mapWidget = displayed.find(widget => widget.type === 'map' && placement(widget) === 'media');
  const mediaWidgets = displayed.filter(widget => widget !== mapWidget && widget.type !== 'video' && placement(widget) === 'media');
  const controlWidgets = displayed.filter(widget => widget.type !== 'video' && placement(widget) === 'controls');
  const hasColorControl = controlWidgets.some(widget => widget.type === 'led_color' || widget.type === 'color_swatches');
  const hiddenWidgets = activeWidgets.filter(widget => widget.visible === false);
  const previewTarget = selectedState || current;
  const view = sm?.views?.[previewTarget];
  const showPreview = connected && (view?.type === 'robot_pose' || view?.type === 'image');
  const renderWidget = widget => <div key={`${widget.type}-${widget._index}`}
    className={`device-board__widget device-board__widget--${placement(widget)}`}>
    {window.fsmWidget?.(unitId, current, [widget], report, actor, liveState, stateField, connectionField, connected)}
  </div>;

  // A new state starts with the graph centered in its own draggable viewport.
  useEffect(() => {
    drag.current = null;
    pointers.current.clear();
    pinch.current = null;
    setPan({ x: 0, y: 0 });
    setZoom(1);
  }, [current]);

  const startPan = event => {
    if (event.button !== 0 || event.target.closest('[role="button"], button')) return;
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, pan };
    event.currentTarget.setPointerCapture(event.pointerId);
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      pinch.current = { distance: Math.hypot(a.x - b.x, a.y - b.y), zoom };
      drag.current = null;
    }
  };
  const movePan = event => {
    if (pointers.current.has(event.pointerId)) {
      pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    }
    if (pinch.current && pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      const distance = Math.hypot(a.x - b.x, a.y - b.y);
      setZoom(Math.max(0.85, Math.min(1.2, pinch.current.zoom * distance / Math.max(1, pinch.current.distance))));
      return;
    }
    if (drag.current?.id !== event.pointerId) return;
    setPan({
      x: Math.max(-180, Math.min(180, drag.current.pan.x + event.clientX - drag.current.x)),
      y: Math.max(-150, Math.min(150, drag.current.pan.y + event.clientY - drag.current.y)),
    });
  };
  const endPan = event => {
    pointers.current.delete(event.pointerId);
    if (pointers.current.size < 2) pinch.current = null;
    if (drag.current?.id === event.pointerId) drag.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };
  return <div className="device-board" style={{ '--board-accent': color }}>
    {(videoWidgets.length > 0 || mapWidget || showPreview) && <div className="device-board__media-row">
      {videoWidgets.length > 0 && <div className="device-board__video">
        {videoWidgets.map(renderWidget)}
      </div>}
      {mapWidget && <div className="device-board__map">{renderWidget(mapWidget)}</div>}
      {showPreview && <div className="device-board__preview">
        <StateVisual view={view} state={previewTarget} color={color}/>
      </div>}
    </div>}
    <div className="device-board__viewport"
      onPointerDown={startPan} onPointerMove={movePan} onPointerUp={endPan} onPointerCancel={endPan}>
      <div className="device-board__canvas" style={{ transform: `translate(calc(-50% + ${pan.x}px), calc(-50% + ${pan.y}px)) scale(${zoom})` }}>
        {sm ? <FsmGraph states={sm.states} transitions={sm.transitions} current={current}
          onFire={onFire} color={color} views={sm.views || {}} layout={sm.layout || {}}
          onSelectState={onSelectState} selectedState={selectedState}/>
          : <div className="device-state-workspace__fallback">{current || '—'}</div>}
      </div>
    </div>
    {hiddenWidgets.map(widget => <React.Fragment key={`hidden-${widget._index}`}>
      {window.fsmWidget?.(unitId, current, [widget], report, actor, liveState, stateField, connectionField, connected)}
    </React.Fragment>)}
    {(mediaWidgets.length > 0 || controlWidgets.length > 0) && <div className={`device-board__widget-tray${hasColorControl ? ' device-board__widget-tray--color' : ''}`}>
      {mediaWidgets.map(renderWidget)}
      {controlWidgets.map(renderWidget)}
    </div>}
    {legacyLed && <div className="device-board__footer">
      <span className="device-board__legacy">旧版状态机 · 固件待更新</span>
    </div>}
  </div>;
}

function DeviceSettingsMenu({ open, onClose, modes, modeVals, onSwitchMode, offline, connected, safetyAction, onSafety, isGo2, isLamp, actor, unitId, onGo2Settings, onLampSettings }) {
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
    <section ref={panelRef} className="device-settings-panel" role="dialog" aria-modal="true" aria-label="设备设置">
      <header><h2>设备设置</h2><button type="button" className="btn" onClick={onClose} aria-label="关闭设备设置"><Icon name="x" size={15}/></button></header>
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
        <span><strong>Go2 设置</strong><small>地点、人设与记忆</small></span><Icon name="arrow" size={15}/>
      </button>}
      {actor === 'admin' && isLamp && <button type="button" className="device-settings-go2" onClick={() => { onClose(); onLampSettings(); }}>
        <span><strong>灯智能体设置</strong><small>人设与记忆</small></span><Icon name="arrow" size={15}/>
      </button>}
      <a className="device-settings-card" href={'/api/devices/' + encodeURIComponent(unitId) + '/agent'}
        target="_blank" rel="noopener noreferrer">
        <span><strong>Agent Card</strong><small>查看设备能力、状态机和控件声明</small></span><Icon name="arrow" size={15}/>
      </a>
    </section>
  </div>;
}
