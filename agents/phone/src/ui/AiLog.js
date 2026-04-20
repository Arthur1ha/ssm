/**
 * AiLog.js — AI 决策日志面板
 *
 * 订阅 PC LangGraph 层发布的两类消息：
 *   ssm/decision/rule_fired  → 决策智能体的决定
 *   ssm/decision/evaluation  → 评估智能体的结论
 *
 * 让用户看到 LLM 在做什么，体现 AI 自治 + 人类监督。
 */

function initAiLog(bus) {
    const logEl   = document.getElementById('ai-log');
    const clearBtn = document.getElementById('btn-ai-log-clear');
    if (!logEl) return;

    const MAX = 50;

    bus.subscribe('ssm/decision/rule_fired');
    bus.subscribe('ssm/decision/evaluation');

    bus.onMessage(({ topic, msg }) => {
        if (topic !== 'ssm/decision/rule_fired' && topic !== 'ssm/decision/evaluation') return;

        const time     = new Date().toLocaleTimeString('zh-CN', { hour12: false });
        const isDecision = topic.includes('rule_fired');
        const label    = isDecision ? '决策' : '评估';
        const cls      = isDecision ? 'ai-decision' : 'ai-evaluation';

        let content = '';
        if (isDecision && typeof msg === 'object') {
            const action = msg.action || {};
            const cmd    = action.cmd || '?';
            const detail = cmd === 'SET_COLOR'
                ? `颜色(${action.r},${action.g},${action.b})`
                : cmd === 'SET_STATE' ? action.state
                : cmd;
            content = `规则 <strong>${msg.rule || '?'}</strong> → ${detail}`;
        } else if (!isDecision && typeof msg === 'object') {
            const icon = msg.result === 'ok' ? '✓' : msg.result === 'blocked' ? '✗' : '~';
            content = `${icon} ${msg.reason || JSON.stringify(msg)}`;
        } else {
            content = typeof msg === 'string' ? msg : JSON.stringify(msg);
        }

        const entry = document.createElement('div');
        entry.className = `ai-entry ${cls}`;
        entry.innerHTML =
            `<span class="ai-time">${time}</span>` +
            `<span class="ai-label">[${label}]</span>` +
            `<span class="ai-content">${content}</span>`;

        logEl.prepend(entry);
        while (logEl.children.length > MAX) logEl.removeChild(logEl.lastChild);
    });

    clearBtn?.addEventListener('click', () => { logEl.innerHTML = ''; });
}
