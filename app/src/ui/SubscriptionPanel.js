/**
 * SubscriptionPanel.js — 智能体订阅面板
 *
 * 每个发现的 unit 生成一张卡片，用户可选择订阅。
 * - 未订阅：只显示身份信息
 * - 订阅后：展开实时数据；执行器额外显示控制按钮
 *
 * 手机在此扮演「人类监督 + Override」角色，控制命令直接发到 command topic，
 * 不经过 PC DecisionAgent，优先级最高。
 */

function initSubscriptionPanel(bus, registry, ismTracker) {
    const container = document.getElementById('subscription-cards');
    if (!container) return;

    const subscribed = new Set();   // 已订阅的 unit_id

    // ── 渲染入口 ──────────────────────────────────────────────

    function render() {
        // 排除已知软件智能体，只展示硬件智能体（ESP32 传感器 + 执行器）
        const EXCLUDED_TYPES = new Set(['decision', 'supervisor']);
        const EXCLUDED_PLATFORMS = new Set(['pc', 'pwa']);
        const agents = registry.getAll().filter(a =>
            !EXCLUDED_TYPES.has(a.agent_type) &&
            !EXCLUDED_PLATFORMS.has(a.hw_platform)
        );
        if (agents.length === 0) {
            container.innerHTML = '<p class="empty-hint">等待 ESP32 上线...</p>';
            return;
        }

        // 保留已存在的卡片 DOM，只更新数据部分，避免控件重建闪烁
        const existingIds = new Set([...container.querySelectorAll('.unit-card')].map(el => el.dataset.unitId));
        const newIds = new Set(agents.map(a => a.unit_id || a.agent_id));

        // 移除消失的卡片
        existingIds.forEach(id => {
            if (!newIds.has(id)) container.querySelector(`[data-unit-id="${id}"]`)?.remove();
        });

        // 新增或更新
        agents.forEach(agent => {
            const uid = agent.unit_id || agent.agent_id;
            let card = container.querySelector(`[data-unit-id="${uid}"]`);
            if (!card) {
                card = buildCard(agent);
                container.appendChild(card);
            } else {
                updateCardHeader(card, agent);
            }
            updateCardBody(card, uid, agent);
        });
    }

    // ── 构建卡片 ──────────────────────────────────────────────

    function buildCard(agent) {
        const uid  = agent.unit_id || agent.agent_id;
        const card = document.createElement('div');
        card.className = 'unit-card';
        card.dataset.unitId = uid;

        // 卡片头（固定不变）
        const header = document.createElement('div');
        header.className = 'unit-card-header';
        header.innerHTML = headerHTML(agent);
        card.appendChild(header);

        // 订阅按钮逻辑
        const btn = header.querySelector('.btn-subscribe');
        btn?.addEventListener('click', () => toggleSubscribe(uid, card, agent));

        // 卡片体（展开内容）
        const body = document.createElement('div');
        body.className = 'unit-card-body';
        card.appendChild(body);

        return card;
    }

    function headerHTML(agent) {
        const uid     = agent.unit_id || agent.agent_id;
        const online  = agent._online;
        const typeLabel = agent.agent_type === 'sensor'   ? '感知'
                        : agent.agent_type === 'actuator' ? '执行'
                        : agent.agent_type || '?';
        return `
        <div class="unit-meta">
            <span class="unit-id">${uid}</span>
            <span class="badge badge-type">${typeLabel}</span>
            <span class="badge badge-name">${agent.name || '?'}</span>
            <span class="status-pill ${online ? 'pill-online' : 'pill-offline'}">
                ${online ? '●' : '○'}
            </span>
        </div>
        <button class="btn-subscribe btn-small">${subscribed.has(uid) ? '取消' : '订阅'}</button>`;
    }

    function updateCardHeader(card, agent) {
        const uid    = agent.unit_id || agent.agent_id;
        const online = agent._online;
        card.querySelector('.status-pill')?.classList.toggle('pill-online',  online);
        card.querySelector('.status-pill')?.classList.toggle('pill-offline', !online);
        if (card.querySelector('.status-pill')) {
            card.querySelector('.status-pill').textContent = online ? '●' : '○';
        }
        card.classList.toggle('subscribed', subscribed.has(uid));
    }

    // ── 卡片体：根据订阅状态和单元类型渲染 ────────────────────

    function updateCardBody(card, uid, agent) {
        const body = card.querySelector('.unit-card-body');
        if (!body) return;

        if (!subscribed.has(uid)) {
            body.innerHTML = '';
            body.style.display = 'none';
            return;
        }
        body.style.display = 'block';

        if (agent.agent_type === 'sensor') {
            renderSensorBody(body, uid);
        } else if (agent.agent_type === 'actuator') {
            // 只在首次展开时建立控件 DOM，之后只更新数据
            if (!body.dataset.built) {
                renderActuatorBody(body, uid, agent);
                body.dataset.built = '1';
            }
            refreshActuatorData(body, uid);
        }
    }

    function renderSensorBody(body, uid) {
        const stateData  = ismTracker.get(uid, 'state')  || {};
        const reportData = ismTracker.get(uid, 'report') || {};

        let stateText = '—';
        if (stateData.level)    stateText = stateData.level;
        else if (stateData.presence !== undefined) stateText = stateData.presence ? '有人' : '无人';
        else if (stateData.detected) stateText = '检测到声音';

        const reportText = reportData.level || (reportData.presence !== undefined
            ? (reportData.presence ? '有人' : '无人') : '');

        body.innerHTML = `
        <div class="sensor-data">
            <div class="data-row"><span class="data-label">当前状态</span><strong>${stateText}</strong></div>
            ${reportText ? `<div class="data-row"><span class="data-label">观测报告</span><span>${reportText}</span></div>` : ''}
            ${stateData.ts ? `<div class="data-row"><span class="data-label">更新时间</span><span>${tsToTime(stateData.ts)}</span></div>` : ''}
        </div>`;
    }

    function renderActuatorBody(body, uid, agent) {
        const name = agent.name;

        if (name === 'rgb_led') {
            body.innerHTML = `
            <div class="actuator-data">
                <div class="data-row">
                    <span class="data-label">ISM 状态</span>
                    <strong class="ism-val">—</strong>
                    <span class="report-val" style="font-size:11px;color:#888;margin-left:8px"></span>
                </div>
            </div>
            <div class="led-control">
                <div class="led-preview-row">
                    <div class="led-preview-box"></div>
                    <div class="slider-group">
                        <label>R <input type="range" min="0" max="255" value="255" data-ch="r"></label>
                        <label>G <input type="range" min="0" max="255" value="255" data-ch="g"></label>
                        <label>B <input type="range" min="0" max="255" value="255" data-ch="b"></label>
                        <label>亮度 <input type="range" min="0" max="255" value="200" data-ch="bri"></label>
                    </div>
                </div>
                <div class="btn-row">
                    <button class="btn-primary btn-send-color">发送</button>
                    <button class="btn-warm btn-warm-color">暖白</button>
                    <button class="btn-off  btn-led-off">关闭</button>
                </div>
            </div>`;
            attachLedHandlers(body, uid, agent);
        }

        else if (name === 'buzzer') {
            body.innerHTML = `
            <div class="actuator-data">
                <div class="data-row">
                    <span class="data-label">ISM 状态</span>
                    <strong class="ism-val">—</strong>
                    <span class="report-val" style="font-size:11px;color:#888;margin-left:8px"></span>
                </div>
            </div>
            <div class="btn-row" style="margin-top:10px">
                <button class="btn-primary btn-buz-notify">通知音</button>
                <button class="btn-warn   btn-buz-alert">警报音</button>
                <button class="btn-off    btn-buz-stop">停止</button>
            </div>`;
            attachBuzzerHandlers(body, uid, agent);
        }
    }

    function refreshActuatorData(body, uid) {
        const stateData  = ismTracker.get(uid, 'state')  || {};
        const reportData = ismTracker.get(uid, 'report') || {};
        const ismVal  = body.querySelector('.ism-val');
        const repVal  = body.querySelector('.report-val');
        if (ismVal)  ismVal.textContent  = stateData.ism || '—';
        if (repVal)  repVal.textContent  = reportData.cmd
            ? `${reportData.cmd} → ${reportData.result}` : '';
    }

    // ── LED 控件事件绑定 ───────────────────────────────────────

    function attachLedHandlers(body, uid, agent) {
        const cmdTopic = agent.topics?.command;
        const preview  = body.querySelector('.led-preview-box');
        const sliders  = body.querySelectorAll('input[type=range]');

        function rgb() {
            return {
                r:   parseInt(body.querySelector('[data-ch=r]').value),
                g:   parseInt(body.querySelector('[data-ch=g]').value),
                b:   parseInt(body.querySelector('[data-ch=b]').value),
                bri: parseInt(body.querySelector('[data-ch=bri]').value),
            };
        }
        function syncPreview() {
            const { r, g, b, bri } = rgb();
            const s = bri / 255;
            preview.style.backgroundColor =
                `rgb(${Math.round(r*s)},${Math.round(g*s)},${Math.round(b*s)})`;
        }
        sliders.forEach(s => s.addEventListener('input', syncPreview));
        syncPreview();

        body.querySelector('.btn-send-color')?.addEventListener('click', () => {
            if (!cmdTopic) return;
            const { r, g, b, bri } = rgb();
            bus.publish(cmdTopic, { cmd: 'SET_COLOR', r, g, b, brightness: bri });
        });
        body.querySelector('.btn-warm-color')?.addEventListener('click', () => {
            if (!cmdTopic) return;
            bus.publish(cmdTopic, { cmd: 'SET_COLOR', r: 255, g: 160, b: 60, brightness: 180 });
            body.querySelector('[data-ch=r]').value = 255;
            body.querySelector('[data-ch=g]').value = 160;
            body.querySelector('[data-ch=b]').value = 60;
            body.querySelector('[data-ch=bri]').value = 180;
            syncPreview();
        });
        body.querySelector('.btn-led-off')?.addEventListener('click', () => {
            if (cmdTopic) bus.publish(cmdTopic, { cmd: 'SET_STATE', state: 'OFF' });
        });
    }

    // ── 蜂鸣器控件事件绑定 ────────────────────────────────────

    function attachBuzzerHandlers(body, uid, agent) {
        const cmdTopic = agent.topics?.command;
        body.querySelector('.btn-buz-notify')?.addEventListener('click', () => {
            if (cmdTopic) bus.publish(cmdTopic, { cmd: 'PLAY', pattern: 'NOTIFY' });
        });
        body.querySelector('.btn-buz-alert')?.addEventListener('click', () => {
            if (cmdTopic) bus.publish(cmdTopic, { cmd: 'PLAY', pattern: 'ALERT' });
        });
        body.querySelector('.btn-buz-stop')?.addEventListener('click', () => {
            if (cmdTopic) bus.publish(cmdTopic, { cmd: 'STOP' });
        });
    }

    // ── 订阅 toggle ───────────────────────────────────────────

    function toggleSubscribe(uid, card, agent) {
        if (subscribed.has(uid)) {
            subscribed.delete(uid);
        } else {
            subscribed.add(uid);
        }
        const btn = card.querySelector('.btn-subscribe');
        if (btn) btn.textContent = subscribed.has(uid) ? '取消' : '订阅';
        card.classList.toggle('subscribed', subscribed.has(uid));
        updateCardBody(card, uid, agent);
    }

    // ── 工具 ─────────────────────────────────────────────────

    function tsToTime(ts) {
        return new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour12: false });
    }

    // ── 事件监听 ──────────────────────────────────────────────

    registry.addEventListener('change', render);

    ismTracker.addEventListener('update', (e) => {
        const uid = e.detail?.unitId;
        if (!uid || !subscribed.has(uid)) return;
        const card  = container.querySelector(`[data-unit-id="${uid}"]`);
        const agent = registry.get(uid);
        if (card && agent) updateCardBody(card, uid, agent);
    });

    render();
}
