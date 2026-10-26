import json
from types import SimpleNamespace

from cloud.space.constraints import (CURRENT_CONSTRAINTS_TOPIC, ConstraintStore)
from cloud.space.context import get_space_context


class _Space:
    def get_space(self):
        return {"device_ids": ["esp32_desk", "go2"]}


class _Cards:
    def get_runtime_snapshot(self):
        return ({
            "esp32_desk_led": {"unit_id": "esp32_desk_led", "device_id": "esp32_desk",
                                 "agent_type": "actuator", "online": True,
                                 "skills": [{"id": "set_light_state"}],
                                 "state": {"ts": 1000, "ism": "ON"}},
            "go2": {"unit_id": "go2", "device_id": "go2", "agent_type": "robot",
                    "online": True, "skills": [{"id": "go2_sport"}],
                    "state": {"ts": 900, "connected": True}},
        }, {"esp32_desk": 1000, "go2": 900})


class _State:
    def sensor_snapshot(self):
        return {"esp32_desk_light": {"state": {"ts": 1000, "level": "DIM"}},
                "esp32_desk_sound": {"state": {"ts": 900, "detected": True}}}

    def actuator_snapshot(self):
        return {"esp32_desk_led": {"state": {"ts": 1000, "ism": "ON", "brightness": 100}}}


def test_space_context_keeps_freshness_and_scoped_requirement(monkeypatch):
    monkeypatch.setattr("cloud.peers.view.get_space_registry", lambda: _Space())
    constraints = ConstraintStore()
    assert constraints.upsert({"constraint_id": "c1", "text": "请安静一点",
                               "source_session_id": "s1", "space_id": "main",
                               "targets": ["esp32_desk_led"], "issued_at": 999,
                               "expires_at": 1100}, now=1000)
    result = get_space_context("esp32_desk_led", state=_State(), registry=_Cards(),
                               constraints=constraints, now=1000)
    assert set(result) == {"peers", "environment", "constraints"}
    assert {peer["unit_id"] for peer in result["peers"]} == {"go2", "esp32_desk_led"}
    model_view = get_space_context("esp32_desk_led", state=_State(), registry=_Cards(),
                                   constraints=constraints, now=1000, include_self=False)
    assert [peer["unit_id"] for peer in model_view["peers"]] == ["go2"]
    assert model_view["environment"] == result["environment"]
    assert model_view["constraints"] == result["constraints"]
    assert next(peer for peer in result["peers"] if peer["unit_id"] == "go2")["state_summary"]["status"] == "stale"
    assert next(item for item in result["environment"] if item["source"] == "esp32_desk_light")["values"] == {"level": "DIM"}
    assert next(item for item in result["environment"] if item["source"] == "esp32_desk_sound")["status"] == "stale"
    assert [item["text"] for item in result["constraints"]] == ["请安静一点"]
    assert get_space_context("go2", state=_State(), registry=_Cards(),
                             constraints=constraints, now=1000)["constraints"] == []


def test_constraint_topic_requires_signature_and_expires(monkeypatch):
    monkeypatch.setenv("SSM_MQTT_SIGNING_SECRET", "constraint-test-secret-123456")
    writer, reader = ConstraintStore(), ConstraintStore()
    assert writer.upsert({"constraint_id": "c1", "text": "请安静一点",
                          "source_session_id": "s1", "space_id": "main",
                          "targets": ["*"], "issued_at": 1000,
                          "expires_at": 1060}, now=1000)

    class _Mqtt:
        payload = None

        def publish(self, topic, payload, *, qos, retain):
            assert topic == CURRENT_CONSTRAINTS_TOPIC and qos == 1 and retain
            self.payload = payload
            return type("Info", (), {"rc": 0})()

    mqtt = _Mqtt()
    assert writer.publish_current(mqtt, now=1000)
    assert not reader.handle_message(CURRENT_CONSTRAINTS_TOPIC,
                                     json.dumps({"constraints": []}), now=1000)
    assert reader.handle_message(CURRENT_CONSTRAINTS_TOPIC, mqtt.payload, now=1000)
    assert len(reader.list_for("go2", now=1000)) == 1
    assert reader.list_for("go2", now=1061) == []
    assert writer.prune_expired(now=1061)
    assert writer.publish_current(mqtt, now=1061)
    assert reader.handle_message(CURRENT_CONSTRAINTS_TOPIC, mqtt.payload, now=1061)
    assert reader.list_for("go2", now=1061) == []


def test_new_writer_revision_after_restart_replaces_old_snapshot(monkeypatch):
    monkeypatch.setenv("SSM_MQTT_SIGNING_SECRET", "constraint-test-secret-123456")
    reader = ConstraintStore()

    class _Mqtt:
        payload = None

        def publish(self, _topic, payload, **_kwargs):
            self.payload = payload
            return type("Info", (), {"rc": 0})()

    mqtt = _Mqtt()
    old_writer = ConstraintStore()
    for i in range(3):
        assert old_writer.upsert({"constraint_id": f"old{i}", "text": "旧要求",
                                  "source_session_id": "s1", "space_id": "main",
                                  "targets": ["*"], "issued_at": 1000,
                                  "expires_at": 1200}, now=1000)
    assert old_writer.publish_current(mqtt, now=1000)
    assert reader.handle_message(CURRENT_CONSTRAINTS_TOPIC, mqtt.payload, now=1000)
    new_writer = ConstraintStore()
    assert new_writer.publish_current(mqtt, now=1001)
    assert reader.handle_message(CURRENT_CONSTRAINTS_TOPIC, mqtt.payload, now=1001)
    assert reader.list_for("go2", now=1001) == []


def test_space_context_accepts_partial_runtime_stubs():
    result = get_space_context("esp32_desk_led",
                               state=SimpleNamespace(actuator_snapshot=lambda: {}),
                               registry=SimpleNamespace(), constraints=ConstraintStore(),
                               now=1000)
    assert result == {"peers": [], "environment": [], "constraints": []}
