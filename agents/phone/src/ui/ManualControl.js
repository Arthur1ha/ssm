/**
 * ManualControl.js — Direct actuator control panel.
 * Discovers LED/buzzer command topics from AgentRegistry (manifest-driven).
 */

function initManualControl(bus, decisionAgent, registry) {

    // Helpers to look up command topics at publish time
    function ledTopic() {
        const a = registry.getAll().find(a => a.name === 'rgb_led' && a.agent_type === 'actuator');
        return a?.topics?.command || null;
    }
    function buzTopic() {
        const a = registry.getAll().find(a => a.name === 'buzzer' && a.agent_type === 'actuator');
        return a?.topics?.command || null;
    }
    function pubLed(payload) {
        const t = ledTopic();
        if (t) bus.publish(t, payload);
        else console.warn('[ManualControl] LED unit not yet discovered');
    }
    function pubBuz(payload) {
        const t = buzTopic();
        if (t) bus.publish(t, payload);
        else console.warn('[ManualControl] Buzzer unit not yet discovered');
    }

    // --- LED Color Picker ---
    const rSlider   = document.getElementById('led-r');
    const gSlider   = document.getElementById('led-g');
    const bSlider   = document.getElementById('led-b');
    const briSlider = document.getElementById('led-brightness');
    const preview   = document.getElementById('led-preview');
    const sendBtn   = document.getElementById('btn-led-send');
    const offBtn    = document.getElementById('btn-led-off');
    const warmBtn   = document.getElementById('btn-led-warm');

    function updatePreview() {
        if (!preview) return;
        const r   = rSlider?.value   || 255;
        const g   = gSlider?.value   || 255;
        const b   = bSlider?.value   || 255;
        const bri = (briSlider?.value || 200) / 255;
        preview.style.backgroundColor =
            `rgb(${Math.round(r*bri)},${Math.round(g*bri)},${Math.round(b*bri)})`;
    }

    [rSlider, gSlider, bSlider, briSlider].forEach(el => {
        el?.addEventListener('input', updatePreview);
    });

    sendBtn?.addEventListener('click', () => {
        pubLed({
            cmd: 'SET_COLOR',
            r:   parseInt(rSlider?.value  || 255),
            g:   parseInt(gSlider?.value  || 255),
            b:   parseInt(bSlider?.value  || 255),
            brightness: parseInt(briSlider?.value || 200)
        });
    });

    offBtn?.addEventListener('click', () => {
        pubLed({ cmd: 'SET_STATE', state: 'OFF' });
    });

    warmBtn?.addEventListener('click', () => {
        pubLed({ cmd: 'SET_COLOR', r: 255, g: 160, b: 60, brightness: 180 });
        if (rSlider)   rSlider.value   = 255;
        if (gSlider)   gSlider.value   = 160;
        if (bSlider)   bSlider.value   = 60;
        if (briSlider) briSlider.value = 180;
        updatePreview();
    });

    // --- Buzzer ---
    document.getElementById('btn-buzz-notify')?.addEventListener('click', () => {
        pubBuz({ cmd: 'PLAY', pattern: 'NOTIFY' });
    });
    document.getElementById('btn-buzz-stop')?.addEventListener('click', () => {
        pubBuz({ cmd: 'STOP' });
    });

    // --- Decision Agent Toggle ---
    const decisionToggle = document.getElementById('toggle-decision');
    const decisionLabel  = document.getElementById('label-decision');

    function syncToggleUI() {
        if (!decisionToggle) return;
        decisionToggle.checked = decisionAgent.enabled;
        if (decisionLabel) {
            decisionLabel.textContent = decisionAgent.enabled
                ? '决策智能体：开启（手机控制）'
                : '决策智能体：关闭（ESP32 自治）';
        }
    }

    decisionToggle?.addEventListener('change', () => {
        if (decisionToggle.checked) decisionAgent.enable();
        else                         decisionAgent.disable();
    });

    decisionAgent.addEventListener('change', syncToggleUI);
    syncToggleUI();
    updatePreview();
}
