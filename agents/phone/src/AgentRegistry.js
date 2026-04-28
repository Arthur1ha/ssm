/**
 * AgentRegistry.js — Discovers and tracks live agents via retained MQTT manifests.
 * Subscribes to ssm/agents/+/manifest, ssm/agents/+/status, ssm/agents/+/location.
 * Emits 'change' events when registry updates.
 */

class AgentRegistry extends EventTarget {
    constructor(bus) {
        super();
        this._agents = new Map();   // agentId → { ...manifest, _online, _lastSeen, _lat, _lng }
        this._bus = bus;

        bus.subscribe('ssm/agents/+/manifest');
        bus.subscribe('ssm/agents/+/status');
        bus.subscribe('ssm/agents/+/location');

        bus.onMessage(({ topic, msg }) => {
            const mManifest = topic.match(/^ssm\/agents\/([^/]+)\/manifest$/);
            const mStatus   = topic.match(/^ssm\/agents\/([^/]+)\/status$/);
            const mLocation = topic.match(/^ssm\/agents\/([^/]+)\/location$/);

            if (mManifest) {
                const id = mManifest[1];
                const existing = this._agents.get(id) || {};
                this._agents.set(id, {
                    ...existing,
                    ...msg,
                    _online:   existing._online === true,   // stay online only if status confirmed it
                    _lastSeen: Date.now()
                });
                this._emit();
            }

            if (mStatus) {
                const id = mStatus[1];
                const online = (msg === 'online');
                const now = Date.now();
                const existing = this._agents.get(id) || { agent_id: id };
                this._agents.set(id, { ...existing, _online: online, _lastSeen: now });
                // propagate to child agents that share this parent_id
                for (const [aid, agent] of this._agents) {
                    if (agent.parent_id === id) {
                        this._agents.set(aid, { ...agent, _online: online, _lastSeen: now });
                    }
                }
                this._emit();
            }

            if (mLocation) {
                const id = mLocation[1];
                const existing = this._agents.get(id) || { agent_id: id };
                this._agents.set(id, { ...existing, _lat: msg.lat, _lng: msg.lng });
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
