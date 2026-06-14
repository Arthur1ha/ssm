/**
 * ISMTracker.js — Tracks current state and events for all agent units.
 * Subscribes to ssm/agents/+/{state,event}.
 * snapshot() returns familiar light/ir/led/buzzer keys by matching unit_id suffix.
 */

class ISMTracker extends EventTarget {
    constructor(bus) {
        super();
        // { unitId: { state: {...}, event: {...} } }
        this._units = {};

        bus.subscribe('ssm/agents/+/state');
        bus.subscribe('ssm/agents/+/event');

        bus.onMessage(({ topic, msg }) => {
            const m = topic.match(/^ssm\/agents\/([^/]+)\/(state|event)$/);
            if (!m) return;

            const [, unitId, msgType] = m;
            if (!this._units[unitId]) this._units[unitId] = {};
            this._units[unitId][msgType] = msg;

            this.dispatchEvent(Object.assign(new Event('update'), { detail: { topic, msg, unitId, msgType } }));
        });
    }

    /**
     * Returns snapshot with familiar keys for DecisionAgent rule evaluation.
     * Finds units by suffix (_light, _ir, _led, _buz).
     */
    snapshot() {
        const find = (suffix) => {
            for (const [uid, data] of Object.entries(this._units)) {
                if (uid.endsWith(suffix)) return data.state || data.event || {};
            }
            return {};
        };
        return {
            light:  find('_light'),
            ir:     find('_ir'),
            led:    find('_led'),
            buzzer: find('_buz'),
            _units: this._units
        };
    }

    /** Get all tracked unit IDs */
    unitIds() { return Object.keys(this._units); }

    /** Get latest data for a specific unit and message type */
    get(unitId, msgType = 'state') {
        return this._units[unitId]?.[msgType];
    }
}
