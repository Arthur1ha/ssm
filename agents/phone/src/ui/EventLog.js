/**
 * EventLog.js — Scrolling real-time log of all ssm/# MQTT messages.
 */

function initEventLog(bus) {
    const logEl  = document.getElementById('event-log');
    const clearBtn = document.getElementById('btn-log-clear');
    if (!logEl) return;

    const MAX_ENTRIES = 100;

    // Subscribe to everything
    bus.subscribe('ssm/#');

    // Color coding by message type suffix or topic prefix
    function topicClass(topic) {
        if (topic.endsWith('/event'))      return 'log-sensor';
        if (topic.endsWith('/report'))     return 'log-actuator';
        if (topic.includes('/decision/')) return 'log-decision';
        if (topic.endsWith('/manifest') || topic.endsWith('/status')) return 'log-agent';
        return 'log-sys';
    }

    bus.onMessage(({ topic, msg }) => {
        const time = new Date().toLocaleTimeString('zh-CN', { hour12: false });
        const payload = typeof msg === 'string' ? msg : JSON.stringify(msg);

        const entry = document.createElement('div');
        entry.className = 'log-entry ' + topicClass(topic);
        entry.innerHTML =
            `<span class="log-time">${time}</span>` +
            `<span class="log-topic">${topic}</span>` +
            `<span class="log-payload">${escapeHtml(payload)}</span>`;

        logEl.prepend(entry);   // newest at top

        // Keep DOM lean
        while (logEl.children.length > MAX_ENTRIES) {
            logEl.removeChild(logEl.lastChild);
        }
    });

    clearBtn?.addEventListener('click', () => { logEl.innerHTML = ''; });
}

function escapeHtml(str) {
    return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
