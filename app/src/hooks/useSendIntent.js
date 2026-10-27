/* useSendIntent — intents enter FastAPI; feedback returns on session-scoped SSE. */
const STAGE_LABELS = { planning: '正在规划', executing: '正在执行', summarizing: '正在汇总', finished: '任务已结束' };

function useSendIntent() {
  const { useState, useCallback, useEffect, useRef } = React;
  const [thinking, setThinking] = useState(false);
  const [thinkingText, setThinkingText] = useState('');
  const activeRef = useRef(null);
  const pendingRunRef = useRef(null);
  const seenRef = useRef(new Set());

  const finish = useCallback(() => {
    const active = activeRef.current;
    if (!active) return;
    clearTimeout(active.timeoutId);
    activeRef.current = null;
    setThinking(false);
    setThinkingText('');
  }, []);

  const resetTimeout = useCallback((milliseconds = 150000) => {
    const active = activeRef.current;
    if (!active) return;
    clearTimeout(active.timeoutId);
    active.timeoutId = setTimeout(() => {
      const onStatus = activeRef.current?.callbacks?.onStatus;
      finish();
      onStatus?.('操作超时，设备可能无响应');
    }, milliseconds);
  }, [finish]);

  useEffect(() => {
    const onEvent = event => {
      const active = activeRef.current;
      if (!active) return;
      const outer = event.detail || {};
      const payload = outer.payload || outer.data || outer;
      if (!active.traceId) {
        if (payload.trace_id && active.pendingEvents.length < 100) active.pendingEvents.push(payload);
        return;
      }
      if (active.traceId && payload.trace_id && payload.trace_id !== active.traceId) return;
      if (payload.event_id && seenRef.current.has(payload.event_id)) return;
      if (payload.event_id) seenRef.current.add(payload.event_id);
      if (payload.type === 'agent_reply') {
        if (payload.text) active.callbacks.onMessage?.(payload.text, payload.agent_id, payload);
        if (payload.agent_id === 'cloud_orchestrator') finish();
        return;
      }
      if (payload.type === 'run_stage') {
        resetTimeout();
        setThinkingText(STAGE_LABELS[payload.stage] || payload.stage || '正在处理');
        active.callbacks.onProgress?.(payload);
        if (payload.stage === 'finished' && payload.status === 'needs_clarification') {
          pendingRunRef.current = payload.run_id || null;
        }
        return;
      }
      if (payload.type === 'agent_progress') {
        resetTimeout();
        active.callbacks.onProgress?.(payload);
        return;
      }
      if (payload.type === 'reply_error') {
        active.callbacks.onStatus?.('回复生成失败，任务状态仍可查询');
        if (payload.agent_id === 'cloud_orchestrator') finish();
        return;
      }
      const stage = payload.stage;
      if (!stage) return;
      const callbacks = active.callbacks;
      if (stage === 'planning' || stage === 'executing') {
        resetTimeout();
        setThinkingText(STAGE_LABELS[stage]);
      } else if (stage === 'ack') {
        resetTimeout();
        setThinkingText(STAGE_LABELS.executing);
      } else if (stage === 'step') {
        resetTimeout();
        const status = payload.result?.result || payload.status;
        const labels = { ok: '已完成', accepted: '已发送，待确认', executing: '执行中',
          blocked: '未执行', rejected: '未执行', timeout: '结果未确认',
          cancelled: '已取消', error: '执行失败' };
        callbacks.onProgress?.({ type: 'agent_progress', status, text: labels[status] || '状态已更新',
          trace_id: payload.trace_id, step_id: payload.step_id, agent_id: 'cloud_orchestrator' });
      } else if (stage === 'question') {
        pendingRunRef.current = payload.run_id || null;
        if (!payload.question) callbacks.onStatus?.('请补充信息');
      } else if (stage === 'pending_rule' && payload.rule) {
        finish();
        callbacks.onPendingRule?.(payload.rule);
      } else if (stage === 'discovery_candidates') {
        finish();
        // Discovery messages inform the user; device onboarding uses its
        // dedicated flow instead of treating a chat event as consent.
        callbacks.onStatus?.(payload.text || '当前空间成员已经显示在设备列表中。');
      } else if (['done', 'partial', 'failed', 'denied'].includes(stage)) {
        pendingRunRef.current = null;
        if (!payload.text) callbacks.onStatus?.(stage === 'done' ? '已处理' : '请求未完成，请查看任务状态');
      }
    };
    apiClient.addEventListener('server-event', onEvent);
    return () => apiClient.removeEventListener('server-event', onEvent);
  }, [finish, resetTimeout]);

  const send = useCallback(async (text, callbacks = {}) => {
    if (!text.trim() || activeRef.current) return;
    setThinking(true);
    setThinkingText(STAGE_LABELS.planning);
    activeRef.current = { callbacks, timeoutId: null, traceId: null, pendingEvents: [] };
    resetTimeout(150000);
    const body = { text: text.trim() };
    if (pendingRunRef.current) body.resume_run_id = pendingRunRef.current;
    if (callbacks.deviceHint) body.device_hint = callbacks.deviceHint;
    if (callbacks.intentHint) body.intent_hint = callbacks.intentHint;
    try {
      const accepted = await apiClient.request('/api/intents', { method: 'POST', body });
      if (activeRef.current) {
        if (accepted.trace_id) {
          activeRef.current.traceId = accepted.trace_id;
          callbacks.onTraceId?.(accepted.trace_id);
          const buffered = activeRef.current.pendingEvents.splice(0);
          buffered.forEach(payload => apiClient.dispatchEvent(Object.assign(
            new Event('server-event'), { detail: payload })));
        }
        if (accepted.stage && ['done', 'partial', 'failed'].includes(accepted.stage)) {
          finish();
          if (!accepted.text) callbacks.onStatus?.(accepted.stage === 'done' ? '已处理' : '请求未完成，请查看任务状态');
        }
      }
    } catch (error) {
      finish();
      callbacks.onStatus?.(apiErrorText(error, '命令没有送达，请稍后再试'));
    }
  }, [finish, resetTimeout]);

  return { thinking, thinkingText, send };
}

window.useSendIntent = useSendIntent;
