/* useSendIntent — 统一 intent 发布 + feedback 监听 hook */
function useSendIntent() {
  const { useState, useCallback, useRef } = React;
  const [thinking, setThinking]         = useState(false);
  const [thinkingText, setThinkingText] = useState('');
  const thinkingRef = useRef(false);

  const send = useCallback((text, { onMessage, onPendingRule, deviceHint } = {}) => {
    if (!text.trim() || thinkingRef.current) return;
    thinkingRef.current = true;
    setThinking(true);
    setThinkingText('正在规划...');

    const session_id    = 'sid_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);
    const feedbackTopic = `ssm/feedback/${session_id}`;
    mqttBus.subscribe(feedbackTopic);

    const cleanup = () => {
      mqttBus.removeEventListener('topic:' + feedbackTopic, handleFeedback);
      mqttBus.unsubscribe(feedbackTopic);
    };

    const done = () => {
      cleanup();
      thinkingRef.current = false;
      setThinking(false);
      setThinkingText('');
    };

    let timeoutId = setTimeout(() => {
      done();
      onMessage?.('操作超时，设备可能无响应');
    }, 60000);

    function handleFeedback(e) {
      const { stage, text: msg, rule } = e.detail || {};
      if (!stage) return;

      if (stage === 'planning' || stage === 'executing') {
        clearTimeout(timeoutId);
        timeoutId = setTimeout(() => {
          done();
          onMessage?.('操作超时，设备可能无响应');
        }, 40000);
        setThinkingText(stage === 'planning' ? '正在规划...' : '正在执行...');
      } else if (stage === 'pending_rule' && rule) {
        clearTimeout(timeoutId);
        done();
        onPendingRule?.(rule);
      } else if (stage === 'done' || stage === 'partial' || stage === 'failed') {
        clearTimeout(timeoutId);
        done();
        onMessage?.(msg || '已处理');
      }
    }

    mqttBus.addEventListener('topic:' + feedbackTopic, handleFeedback);

    const payload = { session_id, user_msg: text, ts: Date.now() };
    if (deviceHint) payload.device_hint = deviceHint;
    mqttBus.publish(`ssm/intent/${session_id}`, JSON.stringify(payload));
  }, []);

  return { thinking, thinkingText, send };
}

window.useSendIntent = useSendIntent;
