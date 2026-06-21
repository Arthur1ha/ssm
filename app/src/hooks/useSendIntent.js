/* useSendIntent — 统一 intent 发布 + feedback 监听 hook */
const PLANNING_TEXTS  = ['思考中…', '理解你的意思…', '让我想想…', '感知中…', '解析指令…', '脑子转起来了…'];
const EXECUTING_TEXTS = ['行动中…', '指令下发…', '启动了…', '开始执行…', '动起来了…'];
const pick = arr => arr[Math.floor(Math.random() * arr.length)];

function useSendIntent() {
  const { useState, useCallback, useRef } = React;
  const [thinking, setThinking]         = useState(false);
  const [thinkingText, setThinkingText] = useState('');
  const thinkingRef = useRef(false);

  const send = useCallback((text, { onMessage, onPendingRule, onDiscoveryCandidates, deviceHint, intentHint } = {}) => {
    if (!text.trim() || thinkingRef.current) return;
    thinkingRef.current = true;
    setThinking(true);
    setThinkingText(pick(PLANNING_TEXTS));

    const session_id    = 'sid_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);
    const feedbackTopic = `ssm/feedback/${session_id}`;
    mqttBus.subscribe(feedbackTopic);

    const cleanup = () => {
      mqttBus.removeEventListener('topic:' + feedbackTopic, handleFeedback);
      mqttBus.unsubscribe(feedbackTopic);
    };

    let finished = false;
    let timeoutId;

    const done = () => {
      if (finished) return;
      finished = true;
      clearTimeout(timeoutId);
      cleanup();
      thinkingRef.current = false;
      setThinking(false);
      setThinkingText('');
    };

    timeoutId = setTimeout(() => {
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
        setThinkingText(stage === 'planning' ? pick(PLANNING_TEXTS) : pick(EXECUTING_TEXTS));
      } else if (stage === 'ack') {
        // Planner 的即时开场白：先上屏一句气泡，本轮还没结束，thinking 继续
        clearTimeout(timeoutId);
        timeoutId = setTimeout(() => {
          done();
          onMessage?.('操作超时，设备可能无响应');
        }, 40000);
        onMessage?.(msg);
        setThinkingText(pick(EXECUTING_TEXTS));
      } else if (stage === 'pending_rule' && rule) {
        clearTimeout(timeoutId);
        done();
        onPendingRule?.(rule);
      } else if (stage === 'discovery_candidates') {
        clearTimeout(timeoutId);
        done();
        onDiscoveryCandidates?.(e.detail?.devices || [], msg || '');
      } else if (stage === 'done' || stage === 'partial' || stage === 'failed') {
        clearTimeout(timeoutId);
        done();
        onMessage?.(msg || '已处理');
      }
    }

    mqttBus.addEventListener('topic:' + feedbackTopic, handleFeedback);

    const payload = { session_id, user_msg: text, ts: Date.now() };
    if (deviceHint) payload.device_hint = deviceHint;
    if (intentHint) payload.intent_hint = intentHint;
    mqttBus.publish(`ssm/intent/${session_id}`, JSON.stringify(payload));
  }, []);

  return { thinking, thinkingText, send };
}

window.useSendIntent = useSendIntent;
