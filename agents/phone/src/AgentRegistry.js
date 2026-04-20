/**
 * AgentRegistry.js — Discovers and tracks live agents via retained MQTT manifests.
 * Subscribes to ssm/agents/+/manifest and ssm/agents/+/status.
 * Emits 'change' events when registry updates.
 */

class AgentRegistry extends EventTarget {
    constructor(bus) {
        super();
        this._agents = new Map();   // agentId → { ...manifest, _online, _lastSeen }
        this._bus = bus;

        bus.subscribe('ssm/agents/+/manifest');
        bus.subscribe('ssm/agents/+/status');

        bus.onMessage(({ topic, msg }) => {
            const mManifest = topic.match(/^ssm\/agents\/([^/]+)\/manifest$/);
            const mStatus   = topic.match(/^ssm\/agents\/([^/]+)\/status$/);

            if (mManifest) {
                const id = mManifest[1];
                const existing = this._agents.get(id) || {};
                this._agents.set(id, {
                    ...existing,
                    ...msg,
                    _online:   existing._online !== false,   // keep last known unless status said offline
                    _lastSeen: Date.now()
                });
                this._emit();
            }

            if (mStatus) {
                const id = mStatus[1];
                const online = (msg === 'online');
                const existing = this._agents.get(id) || { agent_id: id };
                this._agents.set(id, { ...existing, _online: online, _lastSeen: Date.now() });
                this._emit();
            }
        });
    }

    getAll()  { return [...this._agents.values()]; }
    get(id)   { return this._agents.get(id); }
    isOnline(id) { return this._agents.get(id)?._online ?? false; }

    _emit() {
        this.dispatchEvent(new Event('change'));
    }
}
