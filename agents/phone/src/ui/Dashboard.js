/**
 * Dashboard.js — Renders one card per agent unit (sensor + actuator).
 * Shows ISM state from ssm/agents/{unit_id}/state and report feedback.
 */

function renderDashboard(registry, ismTracker) {
    const container = document.getElementById('agent-cards');
    if (!container) return;

    function typeLabel(agentType) {
        return agentType === 'sensor' ? '感知' : agentType === 'actuator' ? '执行' : agentType || '?';
    }

    function stateText(unitId) {
        const s = ismTracker.get(unitId, 'state');
        if (!s) return '—';
        if (s.ism)      return s.ism;                          // actuator: { ism, ts }
        if (s.level)    return s.level;                        // light sensor
        if (s.presence !== undefined) return s.presence ? '有人' : '无人';  // IR
        return JSON.stringify(s);
    }

    function reportText(unitId) {
        const r = ismTracker.get(unitId, 'report');
        if (!r) return null;
        if (r.cmd)   return r.cmd + ' → ' + r.result;         // actuator feedback
        if (r.level) return r.level;                           // sensor observation
        return null;
    }

    function update() {
        const agents = registry.getAll();
        if (agents.length === 0) {
            container.innerHTML = '<p class="empty-hint">暂无在线智能体 — 等待 ESP32 连接...</p>';
            return;
        }

        container.innerHTML = agents.map(agent => {
            const unitId  = agent.unit_id || agent.agent_id;
            const online  = agent._online;
            const ts      = agent._lastSeen
                ? new Date(agent._lastSeen).toLocaleTimeString()
                : '—';
            const curState  = stateText(unitId);
            const lastReport = reportText(unitId);

            return `
            <div class="agent-card ${online ? 'online' : 'offline'}">
                <div class="card-header">
                    <span class="agent-id">${unitId}</span>
                    <span class="badge badge-type">${typeLabel(agent.agent_type)}</span>
                    <span class="badge badge-platform">${agent.name || '?'}</span>
                    <span class="status-pill ${online ? 'pill-online' : 'pill-offline'}">
                        ${online ? '● 在线' : '○ 离线'}
                    </span>
                </div>
                <div class="card-body">
                    <div class="ism-state">状态: <strong>${curState}</strong></div>
                    ${lastReport ? `<div class="last-report">反馈: ${lastReport}</div>` : ''}
                    <div class="last-seen">最后活跃: ${ts}</div>
                </div>
            </div>`;
        }).join('');
    }

    registry.addEventListener('change', update);
    ismTracker.addEventListener('update', update);
    update();
}
