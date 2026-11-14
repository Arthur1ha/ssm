"""Lamp game admission follows real work ownership, without runtime assets."""
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import threading
from types import SimpleNamespace

import pytest

from cloud.esp32.service import ESP32Agent
from cloud.esp32.state import ESP32State


@pytest.fixture
def agent():
    # No profile, memory, MQTT or model construction is needed for admission.
    instance = ESP32Agent.__new__(ESP32Agent)
    instance._state = ESP32State()
    instance._conversation_lock = threading.Lock()
    instance._conversations = 0
    return instance


@pytest.mark.parametrize('status', ['accepted', 'executing', 'timeout', 'unknown'])
def test_uncertain_physical_task_blocks_game_until_real_terminal(agent, status):
    agent._state.mark_task_pending('one')
    agent._state.store_task_result('one', {'result': status})
    assert agent.is_busy_for_game()
    agent._state.store_task_result('one', {'result': 'ok'})
    assert not agent.is_busy_for_game()
    assert agent._state.get_task_result('one')['result'] == 'ok'


@pytest.mark.parametrize('status', ['ok', 'blocked', 'rejected', 'error', 'cancelled'])
def test_fast_terminal_receipt_is_not_reopened_by_task_loopback(agent, status):
    agent._state.store_task_result('one', {'result': status})
    agent._state.mark_task_pending('one')
    assert not agent.is_busy_for_game()
    agent._state.store_task_result('one', {'result': 'executing'})
    assert agent._state.get_task_result('one')['result'] == status


def test_wait_timeout_and_result_cache_eviction_do_not_clear_unconfirmed_dispatch(agent):
    agent._state.mark_task_pending('unconfirmed')
    assert agent._state.wait_task_result('unconfirmed', timeout=0)['result'] == 'timeout'
    for index in range(300):
        agent._state.store_task_result(str(index), {'result': 'ok'})
    assert agent.is_busy_for_game()
    agent._state.store_task_result('unconfirmed', {'result': 'ok'})
    assert not agent.is_busy_for_game()


def test_one_finished_task_does_not_release_another(agent):
    for task_id in ('one', 'two'):
        agent._state.mark_task_pending(task_id)
    agent._state.store_task_result('one', {'result': 'ok'})
    assert agent.is_busy_for_game()
    agent._state.store_task_result('two', {'result': 'cancelled'})
    assert not agent.is_busy_for_game()


def test_two_conversations_remain_busy_until_both_return(agent, monkeypatch):
    graph = importlib.import_module('cloud.agents.lamp.graph')
    entered = [threading.Event(), threading.Event()]
    finish = [threading.Event(), threading.Event()]

    def run(_agent, text, *_args, **_kwargs):
        index = int(text)
        entered[index].set()
        assert finish[index].wait(3)
        return {'result': 'ok'}

    monkeypatch.setattr(graph, 'run_lamp_loop', run)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(agent.run_user_loop, '0', 'lamp', None, None)
        second = workers.submit(agent.run_user_loop, '1', 'lamp', None, None)
        try:
            assert all(event.wait(3) for event in entered)
            assert agent.is_busy_for_game()
            finish[0].set()
            assert first.result(timeout=3)['result'] == 'ok'
            assert agent.is_busy_for_game()
            finish[1].set()
            assert second.result(timeout=3)['result'] == 'ok'
            assert not agent.is_busy_for_game()
        finally:
            for event in finish:
                event.set()


def test_conversation_exception_releases_only_its_ownership(agent, monkeypatch):
    graph = importlib.import_module('cloud.agents.lamp.graph')

    def fail(*_args, **_kwargs):
        assert agent.is_busy_for_game()
        raise RuntimeError('simulated graph failure')

    monkeypatch.setattr(graph, 'run_lamp_loop', fail)
    with pytest.raises(RuntimeError, match='simulated'):
        agent.run_user_loop('test', 'lamp', None, None)
    assert not agent.is_busy_for_game()


def test_training_work_is_busy_but_social_evaluation_is_not(agent):
    agent._social_state = SimpleNamespace(activity_id='current')
    assert not agent.is_busy_for_game()
    agent._training_behavior_run_id = 'training'
    assert agent.is_busy_for_game()
    agent._training_behavior_run_id = None
    assert not agent.is_busy_for_game()


@pytest.fixture
def api_boundary(agent, monkeypatch, tmp_path):
    # Importing the application must not load local credentials for this test.
    import dotenv
    monkeypatch.setattr(dotenv, 'load_dotenv', lambda *_args, **_kwargs: False)
    main = importlib.import_module('cloud.api.main')
    intents = importlib.import_module('cloud.api.intents')
    monkeypatch.setenv('ESP32_LED_UNIT_ID', 'esp32_desk_led')
    monkeypatch.setattr(main, '_esp32_state', agent._state)
    monkeypatch.setattr('cloud.esp32.physical_tasks.PHYSICAL_TASKS_DIR', tmp_path / 'physical')
    return main, intents


def test_dispatch_marks_busy_before_a_fast_result_can_return(agent, api_boundary, monkeypatch):
    main, intents = api_boundary
    from cloud.cards.builder import build_card_from_manifest
    from edge.lamp_contract import FSM_VERSION, capabilities
    card = build_card_from_manifest({
        'unit_id': 'esp32_desk_led', 'name': '测试灯', 'agent_type': 'actuator',
        'firmware_ver': '0.3.0', 'fsm_version': FSM_VERSION, 'capabilities': capabilities(),
    })
    card['online'] = True
    agent._state.on_agent_msg('esp32_desk_led', 'state', {'ism': 'OFF'})
    monkeypatch.setattr(intents, '_configured_card', lambda _unit: card)
    monkeypatch.setattr(intents, 'emit_agent_event', lambda *_args, **_kwargs: None)

    def publish(_topic, payload):
        assert agent.is_busy_for_game()
        agent._state.store_task_result(payload['task_id'], {'result': 'ok'})

    monkeypatch.setattr(intents, '_mqtt_publish', publish)
    task_id = intents._dispatch_mqtt_task('esp32_desk_led', 'SET_STATE', {'state': 'ON'},
                                        {'session_id': 'test', 'actor': 'admin'})
    assert task_id.startswith('task_')
    assert not agent.is_busy_for_game()


@pytest.mark.parametrize('evicted', [False, True])
def test_direct_mqtt_loopback_tracks_only_the_lamp(agent, api_boundary, monkeypatch, evicted):
    main, intents = api_boundary
    # Keep the MQTT callback on its existing dispatch branch without touching
    # discovery, members, activities, durable ledgers or sensor events.
    class Ignore:
        def handle_message(self, *_args, **_kwargs):
            return False

    ignore = Ignore()
    monkeypatch.setattr(main, 'get_registry', lambda: ignore)
    monkeypatch.setattr(main, 'get_constraint_store', lambda: ignore)
    monkeypatch.setattr(main, 'get_activity_store', lambda: ignore)
    monkeypatch.setattr(main, '_agent_messenger', None)
    monkeypatch.setattr(main.peer_gateway, 'handle_peer_result', lambda *_args: False)
    monkeypatch.setattr(intents, 'handle_mqtt_message', lambda *_args: False)

    def send(unit, suffix='a' * 32, **fields):
        payload = {'source': 'peer_game_color', **fields}
        message = SimpleNamespace(topic=f'ssm/task/{unit}/task_{suffix}',
                                  payload=json.dumps(payload).encode(), retain=False)
        main._on_esp32_message(None, None, message)

    send('esp32_desk_sound')
    assert not agent.is_busy_for_game()
    send('esp32_desk_led', task_id='mismatched')
    assert not agent.is_busy_for_game()
    send('esp32_desk_led')
    assert agent.is_busy_for_game()
    task_id = 'task_' + 'a' * 32
    agent._state.store_task_result(task_id, {'result': 'ok'})
    if evicted:
        from cloud.esp32 import physical_tasks
        physical_tasks.reserve(task_id, unit_id='esp32_desk_led', session_id='test',
                               action='SET_STATE', params={'state': 'ON'})
        physical_tasks.record_result(task_id, 'esp32_desk_led', {'result': 'ok'})
        for index in range(300):
            agent._state.store_task_result(str(index), {'result': 'ok'})
        assert agent._state.get_task_result(task_id) is None
    send('esp32_desk_led')
    assert not agent.is_busy_for_game()
