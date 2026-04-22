/**
 * MqttBus.js — MQTT WebSocket connection wrapper
 * Single broker connection for the entire PWA.
 * Acts as an EventEmitter: emit("message", {topic, msg}) for every incoming message,
 * and also emit(topic, msg) for topic-specific listeners.
 */

class MqttBus extends EventTarget {
    constructor() {
        super();
        this._client = null;
        this._subscriptions = new Set();
        this._connected = false;
        this._brokerUrl = null;
    }

    connect(brokerWsUrl, clientId, opts = {}) {
        this._brokerUrl = brokerWsUrl;
        const id = clientId || ('phone_' + Math.random().toString(16).slice(2, 10));

        this._client = mqtt.connect(brokerWsUrl, {
            clientId: id,
            keepalive: 60,
            reconnectPeriod: 3000,
            username: opts.username,
            password: opts.password,
            will: {
                topic: 'ssm/agents/phone_decision/status',
                payload: 'offline',
                retain: true,
                qos: 1
            }
        });

        this._client.on('connect', () => {
            this._connected = true;
            console.log('[MqttBus] Connected to', brokerWsUrl);
            // Re-subscribe after reconnect
            this._subscriptions.forEach(t => this._client.subscribe(t, { qos: 1 }));
            this._dispatch('connect', null);
        });

        this._client.on('reconnect', () => {
            console.log('[MqttBus] Reconnecting...');
            this._dispatch('reconnect', null);
        });

        this._client.on('disconnect', () => {
            this._connected = false;
            this._dispatch('disconnect', null);
        });

        this._client.on('error', (err) => {
            console.error('[MqttBus] Error:', err);
            this._dispatch('error', err);
        });

        this._client.on('message', (topic, payloadBuf) => {
            let msg;
            const raw = payloadBuf.toString();
            try { msg = JSON.parse(raw); }
            catch { msg = raw; }                 // plain string payload (e.g. "online")

            // Fire generic listener
            this._dispatch('message', { topic, msg });
            // Fire topic-specific listener (dots replaced with underscores for safety)
            this._dispatch('topic:' + topic, msg);
        });
    }

    subscribe(topicPattern) {
        this._subscriptions.add(topicPattern);
        if (this._connected) {
            this._client.subscribe(topicPattern, { qos: 1 });
        }
    }

    publish(topic, payload, opts = {}) {
        if (!this._connected) {
            console.warn('[MqttBus] publish skipped — not connected');
            return;
        }
        const data = typeof payload === 'string' ? payload : JSON.stringify(payload);
        this._client.publish(topic, data, {
            retain: opts.retain || false,
            qos:    opts.qos    !== undefined ? opts.qos : 1
        });
    }

    onMessage(handler) {
        this.addEventListener('message', e => handler(e.detail));
    }

    onTopic(topic, handler) {
        this.addEventListener('topic:' + topic, e => handler(e.detail));
    }

    onConnect(handler) {
        this.addEventListener('connect', handler);
    }

    get connected() { return this._connected; }

    _dispatch(type, detail) {
        this.dispatchEvent(Object.assign(new Event(type), { detail }));
    }
}

// Singleton
const mqttBus = new MqttBus();
