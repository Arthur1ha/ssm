/* ChatSheet — 主页底部弹出对话 sheet，负责总控编排意图 */
const SUGGESTIONS = ['我要工作了', '帮我营造睡眠氛围', '有人来了', '我要离开了'];

function ChatSheet({ open, onClose, agents, unitData, messages, onAppend }) {
  const { useState, useEffect, useRef } = React;

  const actuatorsRef = useRef([]);
  actuatorsRef.current = agents.filter(a => a.agent_type === 'actuator');
  const subs = actuatorsRef.current;

  const [thinking, setThinking]       = useState(false);
  const [thinkingText, setThinkingText] = useState('');
  const [kbOffset, setKbOffset]       = useState(0);
  const [pendingRule, setPendingRule]  = useState(null);
  const [savingRule, setSavingRule]    = useState(false);

  useEffect(() => {
    if (!open) setKbOffset(0);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const vv = window.visualViewport;
    if (!vv) return;
    const update = () => setKbOffset(Math.max(0, window.innerHeight - vv.height));
    vv.addEventListener('resize', update);
    vv.addEventListener('scroll', update);
    return () => { vv.removeEventListener('resize', update); vv.removeEventListener('scroll', update); };
  }, [open]);

  const handleConfirmRule = async () => {
    if (!pendingRule) return;
    setSavingRule(true);
    try {
      await fetch('/api/rules', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(pendingRule),
      });
      const saved = pendingRule;
      setPendingRule(null);
      onAppend({ role: 'assistant', text: `规则「${saved.name}」已保存，条件触发时自动执行。`, actions: [] });
    } catch {
      onAppend({ role: 'assistant', text: '规则保存失败，请重试。', actions: [] });
    }
    setSavingRule(false);
  };

  const handleCancelRule = () => {
    setPendingRule(null);
    onAppend({ role: 'assistant', text: '已取消，规则未保存。', actions: [] });
  };

  const send = async (text) => {
    const t = text.trim();
    if (!t) return;
    setPendingRule(null);
    onAppend({ role: 'user', text: t });

    if (subs.length === 0) {
      onAppend({ role: 'assistant', text: '附近没有发现可控设备，请确认 ESP32 已上线。', actions: [] });
      return;
    }

    setThinking(true);
    setThinkingText('正在规划...');

    const session_id = 'sid_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);
    const feedbackTopic = `ssm/feedback/${session_id}`;
    mqttBus.subscribe(feedbackTopic);

    const cleanup = () => {
      mqttBus.removeEventListener('topic:' + feedbackTopic, handleFeedback);
      mqttBus.unsubscribe(feedbackTopic);
    };

    let timeoutId = setTimeout(() => {
      cleanup();
      setThinking(false);
      setThinkingText('');
      onAppend({ role: 'assistant', text: '操作超时，设备可能无响应', actions: [] });
    }, 60000);

    function handleFeedback(e) {
      const { stage, text, rule } = e.detail || {};
      if (!stage) return;
      if (stage === 'planning' || stage === 'executing') {
        clearTimeout(timeoutId);
        timeoutId = setTimeout(() => {
          cleanup();
          setThinking(false);
          setThinkingText('');
          onAppend({ role: 'assistant', text: '操作超时，设备可能无响应', actions: [] });
        }, 40000);
        setThinkingText(stage === 'planning' ? '正在规划...' : '正在执行...');
      } else if (stage === 'pending_rule' && rule) {
        clearTimeout(timeoutId);
        cleanup();
        setThinking(false);
        setThinkingText('');
        setPendingRule(rule);
      } else if (stage === 'done' || stage === 'partial' || stage === 'failed') {
        clearTimeout(timeoutId);
        cleanup();
        setThinking(false);
        setThinkingText('');
        onAppend({ role: 'assistant', text, actions: [] });
      }
    }
    mqttBus.addEventListener('topic:' + feedbackTopic, handleFeedback);

    mqttBus.publish(`ssm/intent/${session_id}`, JSON.stringify({
      session_id,
      user_msg: t,
      ts: Date.now(),
    }));
  };

  return (
    <>
      <div onClick={onClose} style={{
        position: 'fixed', inset: 0, zIndex: 200,
        background: 'rgba(0,0,0,0.55)',
        opacity: open ? 1 : 0,
        transition: 'opacity 0.3s',
        pointerEvents: open ? 'auto' : 'none',
      }}/>
      <div style={{
        position: 'fixed', left: 0, right: 0,
        bottom: kbOffset,
        height: kbOffset > 0 ? `calc(100vh - ${kbOffset}px - 20px)` : '82vh',
        zIndex: 201,
        background: '#0F0F14',
        borderRadius: '22px 22px 0 0',
        border: '1px solid rgba(255,255,255,0.08)',
        boxShadow: '0 -20px 60px rgba(0,0,0,0.5)',
        transform: open ? 'translateY(0)' : 'translateY(100%)',
        transition: 'transform 0.38s cubic-bezier(0.32, 0.72, 0, 1), bottom 0.22s ease, height 0.22s ease',
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
      }}>
        <div style={{ padding: '10px 0 2px', display: 'flex', justifyContent: 'center', flexShrink: 0 }}>
          <div style={{ width: 36, height: 4, borderRadius: 2, background: 'rgba(255,255,255,0.12)' }}/>
        </div>
        <div style={{ padding: '6px 20px 10px', display: 'flex', alignItems: 'center', gap: 10,
          flexShrink: 0, borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
          <div style={{ width: 30, height: 30, borderRadius: 10, background: LIME, color: '#0B0B0E',
            display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 16, flexShrink: 0 }}>◐</div>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 14, fontWeight: 500 }}>SSM 助手</div>
            <div style={{ fontSize: 11, color: LIME, fontFamily: 'monospace' }}>{subs.length} 个设备</div>
          </div>
          <button onClick={onClose} style={{
            width: 30, height: 30, borderRadius: '50%', border: '1px solid rgba(255,255,255,0.09)',
            background: 'rgba(255,255,255,0.06)', color: 'rgba(255,255,255,0.6)',
            cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 0,
          }}>
            <Icon name="x" size={14}/>
          </button>
        </div>
        <ChatPanel
          messages={messages}
          thinking={thinking}
          thinkingText={thinkingText}
          onSend={send}
          placeholder="告诉我想要什么…"
          variant="sheet"
          open={open}
        >
          {pendingRule && (
            <div style={{ padding: '0 16px 10px', flexShrink: 0 }}>
              <div style={{ background: 'rgba(200,255,62,0.07)', border: '1px solid rgba(200,255,62,0.22)',
                borderRadius: 18, padding: '14px 16px' }}>
                <div style={{ fontSize: 12, color: LIME, fontWeight: 600, marginBottom: 8 }}>规则预览 · 确认保存？</div>
                <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 4 }}>{pendingRule.name}</div>
                <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.4)', fontFamily: 'monospace', marginBottom: 12 }}>
                  当 {pendingRule.trigger?.agent_tag}.{pendingRule.trigger?.event}
                  {' → '}
                  {pendingRule.action?.resource_tag} / {pendingRule.action?.cmd}
                </div>
                <div style={{ display: 'flex', gap: 8 }}>
                  <button onClick={handleCancelRule}
                    style={{ flex: 1, padding: '9px 0', borderRadius: 999, fontSize: 13,
                      background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.09)',
                      color: 'rgba(255,255,255,0.5)', cursor: 'pointer', fontFamily: 'inherit' }}>
                    取消
                  </button>
                  <button onClick={handleConfirmRule} disabled={savingRule}
                    style={{ flex: 2, padding: '9px 0', borderRadius: 999, fontSize: 13, fontWeight: 600,
                      background: LIME, border: 'none', color: '#0B0B0E',
                      cursor: 'pointer', fontFamily: 'inherit',
                      boxShadow: '0 0 16px rgba(200,255,62,0.3)' }}>
                    {savingRule ? '保存中...' : '确认保存'}
                  </button>
                </div>
              </div>
            </div>
          )}
          {messages.length <= 1 && (
            <div style={{ padding: '0 12px 8px', display: 'flex', gap: 6,
              overflowX: 'auto', scrollbarWidth: 'none', flexShrink: 0 }}>
              {SUGGESTIONS.map(s => (
                <button key={s} onClick={() => send(s)} style={{
                  padding: '6px 12px', borderRadius: 999, whiteSpace: 'nowrap',
                  background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.09)',
                  color: 'rgba(255,255,255,0.6)', fontSize: 12, cursor: 'pointer', fontFamily: 'inherit',
                }}>{s}</button>
              ))}
            </div>
          )}
        </ChatPanel>
      </div>
    </>
  );
}

window.ChatSheet = ChatSheet;
