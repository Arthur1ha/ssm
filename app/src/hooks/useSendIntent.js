/* useSendIntent — intents enter FastAPI; feedback returns on session-scoped SSE. */
const PLANNING_TEXTS  = ['思考中…', '理解你的意思…', '让我想想…', '感知中…', '解析指令…', '脑子转起来了…'];
const EXECUTING_TEXTS = ['行动中…', '指令下发…', '启动了…', '开始执行…', '动起来了…'];
const pick = arr => arr[Math.floor(Math.random() * arr.length)];

function useSendIntent() {
  const { useState, useCallback, useEffect, useRef } = React;
  const [thinking, setThinking] = useState(false);
  const [thinkingText, setThinkingText] = useState('');
  const activeRef = useRef(null);
  const pendingRunRef = useRef(null);

  const finish = useCallback(() => {
    const active = activeRef.current;
    if (!active) return;
    clearTimeout(active.timeoutId);
    activeRef.current = null;
    setThinking(false);
    setThinkingText('');
  }, []);

  const resetTimeout = useCallback((milliseconds = 40000) => {
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
      const stage = payload.stage;
      if (!stage) return;
      const callbacks = active.callbacks;
      if (stage === 'planning' || stage === 'executing') {
        resetTimeout();
        setThinkingText(stage === 'planning' ? pick(PLANNING_TEXTS) : pick(EXECUTING_TEXTS));
      } else if (stage === 'ack') {
        resetTimeout();
        setThinkingText(pick(EXECUTING_TEXTS));
      } else if (stage === 'step') {
        resetTimeout();
        const status = payload.result?.result || payload.status;
        const labels = { ok: '已完成', accepted: '已发送，待确认', executing: '执行中',
          blocked: '未执行', rejected: '未执行', timeout: '结果未确认',
          cancelled: '已取消', error: '执行失败' };
        callbacks.onStatus?.(`第 ${payload.step_number || '?'} 步：${labels[status] || '状态已更新'}`);
      } else if (stage === 'question') {
        pendingRunRef.current = payload.run_id || null;
        finish();
        const question = payload.question || payload.text;
        if (question) callbacks.onMessage?.(question);
        else callbacks.onStatus?.('请补充信息');
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
        finish();
        if (payload.text) callbacks.onMessage?.(payload.text);
        else callbacks.onStatus?.(stage === 'done' ? '已处理' : '请求未完成，请查看任务状态');
      }
    };
    apiClient.addEventListener('server-event', onEvent);
    return () => apiClient.removeEventListener('server-event', onEvent);
  }, [finish, resetTimeout]);

  const send = useCallback(async (text, callbacks = {}) => {
    if (!text.trim() || activeRef.current) return;
    setThinking(true);
    setThinkingText(pick(PLANNING_TEXTS));
    activeRef.current = { callbacks, timeoutId: null };
    resetTimeout(60000);
    const body = { text: text.trim() };
    if (pendingRunRef.current) body.resume_run_id = pendingRunRef.current;
    if (callbacks.deviceHint) body.device_hint = callbacks.deviceHint;
    if (callbacks.intentHint) body.intent_hint = callbacks.intentHint;
    try {
      const accepted = await apiClient.request('/api/intents', { method: 'POST', body });
      if (activeRef.current) {
        if (accepted.trace_id) callbacks.onTraceId?.(accepted.trace_id);
        if (accepted.stage && ['done', 'partial', 'failed'].includes(accepted.stage)) {
          finish();
          if (accepted.text) callbacks.onMessage?.(accepted.text);
          else callbacks.onStatus?.(accepted.stage === 'done' ? '已处理' : '请求未完成，请查看任务状态');
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
