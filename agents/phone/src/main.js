/**
 * main.js — 应用入口
 * 手机角色：人类监督 + Override 层
 */

const BROKER_WS_URL = 'ws://10.193.37.44:9001';

document.addEventListener('DOMContentLoaded', () => {
    const statusEl    = document.getElementById('connection-status');
    const brokerInput = document.getElementById('broker-url');
    const connectBtn  = document.getElementById('btn-connect');

    if (brokerInput) brokerInput.value = BROKER_WS_URL;

    function setStatus(text, cls) {
        if (!statusEl) return;
        statusEl.textContent = text;
        statusEl.className   = 'status-badge ' + (cls || '');
    }

    // ── 核心服务 ──────────────────────────────────────────────
    const registry   = new AgentRegistry(mqttBus);
    const ismTracker = new ISMTracker(mqttBus);
    const _decision  = new DecisionAgent(mqttBus);   // 仅管理 decision/active 标志

    // ── UI 组件 ───────────────────────────────────────────────
    initSubscriptionPanel(mqttBus, registry, ismTracker);
    initAiLog(mqttBus);
    initEventLog(mqttBus);

    // ── 连接状态 ──────────────────────────────────────────────
    mqttBus.onConnect(() => {
        setStatus('已连接', 'connected');
        mqttBus.publish('ssm/agents/phone_ui/status', 'online', { retain: true });
        mqttBus.publish('ssm/agents/phone_ui/manifest', {
            unit_id:    'phone_ui',
            agent_type: 'supervisor',
            name:       'human_supervisor',
            hw_platform:'pwa',
            topics: { manifest: 'ssm/agents/phone_ui/manifest' }
        }, { retain: true });
    });

    mqttBus.addEventListener('disconnect', () => setStatus('已断开', 'disconnected'));
    mqttBus.addEventListener('reconnect',  () => setStatus('重连中...', 'reconnecting'));
    mqttBus.addEventListener('error',      () => setStatus('连接错误', 'error'));

    connectBtn?.addEventListener('click', () => {
        const url = brokerInput?.value.trim() || BROKER_WS_URL;
        setStatus('连接中...', 'reconnecting');
        mqttBus.connect(url);
    });

    setStatus('连接中...', 'reconnecting');
    mqttBus.connect(BROKER_WS_URL);
});
