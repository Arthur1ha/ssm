/* Intents enter FastAPI; trace-scoped facts and original replies return on SSE. */
function useSendIntent() {
  const {useState,useCallback,useEffect,useRef} = React;
  const [thinking,setThinking] = useState(false);
  const activeRef = useRef(null);
  const latestRef = useRef(null);
  const completedRef = useRef(new Map());
  const pendingRunRef = useRef(null);
  const seenRef = useRef(new Set());

  // Releasing input does not unsubscribe the trace: the original reply can
  // follow its terminal lifecycle event, including after a new request starts.
  const finish = useCallback((request = activeRef.current) => {
    if (!request) return;
    clearTimeout(request.timeoutId);
    if (request.traceId) {
      completedRef.current.set(request.traceId,request);
      while (completedRef.current.size > 20) completedRef.current.delete(completedRef.current.keys().next().value);
    }
    if (activeRef.current === request) {activeRef.current=null;setThinking(false);}
  },[]);
  const resetTimeout = useCallback((request,milliseconds=150000) => {
    if (!request || activeRef.current !== request) return;
    clearTimeout(request.timeoutId);
    request.timeoutId=setTimeout(() => {
      finish(request);
      request.callbacks.onStatus?.('操作超时，设备可能无响应');
    },milliseconds);
  },[finish]);

  useEffect(() => {
    const onEvent = event => {
      const outer=event.detail || {}, payload=outer.payload || outer.data || outer;
      if (payload.scope === 'space') return;
      const active=activeRef.current;
      if (active && !active.traceId) {
        if (payload.trace_id && active.pendingEvents.length < 100) active.pendingEvents.push(payload);
        return;
      }
      if (!payload.trace_id) return;
      const request=active?.traceId === payload.trace_id ? active : completedRef.current.get(payload.trace_id);
      if (!request) return;
      // A replaced goal may still receive a physical receipt. Keep that fact
      // on its original request, without reviving its plans or final speech.
      if (request.superseded && !(['run_stage','agent_progress'].includes(payload.type)
          && (payload.fact_kind === 'result' || payload.public_result))) return;
      const source=payload.source_event_id || payload.event_id;
      const identity=source ? `${payload.trace_id}:${source}` : '';
      if (identity && seenRef.current.has(identity)) return;
      if (identity) {
        seenRef.current.add(identity);
        while(seenRef.current.size>500) seenRef.current.delete(seenRef.current.values().next().value);
      }
      const current=latestRef.current === request;
      const awaiting=payload.status === 'needs_clarification' || payload.stage === 'question';
      if (current && awaiting) pendingRunRef.current=payload.run_id || null;
      if (payload.type === 'agent_reply') {
        if (payload.text) request.callbacks.onMessage?.(payload.text,payload.agent_id,payload);
        if (['cloud_orchestrator','orchestrator'].includes(payload.agent_id)) finish(request);
        return;
      }
      if (['run_stage','agent_progress'].includes(payload.type)) {
        request.callbacks.onProgress?.(payload);
        const terminal=['finished','done','partial','failed','denied','question'].includes(payload.stage);
        if (terminal && ['cloud_orchestrator','orchestrator'].includes(payload.agent_id)) {
          if (current && !awaiting) pendingRunRef.current=null;
          finish(request);
        } else resetTimeout(request);
        return;
      }
      if (payload.type === 'reply_error') {
        request.callbacks.onStatus?.(payload.text || '回复生成失败');
        if (['cloud_orchestrator','orchestrator'].includes(payload.agent_id)) finish(request);
        return;
      }
    };
    apiClient.addEventListener('server-event',onEvent);
    return () => {
      apiClient.removeEventListener('server-event',onEvent);
      clearTimeout(activeRef.current?.timeoutId);
      completedRef.current.forEach(request=>clearTimeout(request.timeoutId));
    };
  },[finish,resetTimeout]);

  const send=useCallback(async (text,callbacks={}) => {
    if (!text.trim()) return;
    const previous=activeRef.current;
    if (previous) {
      previous.superseded=true;
      previous.callbacks.onSuperseded?.();
      finish(previous);
      pendingRunRef.current=null;
    }
    const request={callbacks,timeoutId:null,traceId:null,pendingEvents:[]};
    activeRef.current=request;latestRef.current=request;setThinking(true);
    resetTimeout(request);
    const body={text:text.trim()};
    if (pendingRunRef.current) body.resume_run_id=pendingRunRef.current;
    if (callbacks.selection) body.selection=callbacks.selection;
    if (callbacks.deviceHint) body.device_hint=callbacks.deviceHint;
    if (callbacks.intentHint) body.intent_hint=callbacks.intentHint;
    try {
      const accepted=await apiClient.request('/api/intents',{method:'POST',body});
      request.traceId=accepted.trace_id || null;
      if (request.traceId) {
        if (request.superseded) {
          completedRef.current.set(request.traceId,request);
          while (completedRef.current.size > 20) completedRef.current.delete(completedRef.current.keys().next().value);
        }
        callbacks.onTraceId?.(request.traceId,accepted.event_id);
        const buffered=request.pendingEvents.splice(0);
        buffered.forEach(payload=>apiClient.dispatchEvent(Object.assign(new Event('server-event'),{detail:payload})));
      }
    } catch(error) {
      finish(request);
      if (!request.superseded) callbacks.onStatus?.(apiErrorText(error,'命令没有送达，请稍后再试'));
    }
  },[finish,resetTimeout]);
  return {thinking,send};
}
window.useSendIntent=useSendIntent;
