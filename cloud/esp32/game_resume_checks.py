"""Game output survives stale reactive observations without freezing autonomy."""
from types import SimpleNamespace
import threading

import pytest

from cloud.esp32.service import ESP32Agent
from cloud.esp32.state import ESP32State
from cloud.agents.lamp.workflows import reactive


@pytest.fixture
def agent(monkeypatch):
    from cloud.agents.lamp.personality import profile
    from cloud.peers import gateway, social
    from cloud.space import context

    monkeypatch.setattr(profile, 'get_profile', lambda: {
        'revision': 1, 'traits': {'adjustment_sensitivity': 50, 'social_initiative': 70}})
    monkeypatch.setattr(gateway, 'list_visible_peers', lambda _unit: [])
    monkeypatch.setattr(gateway, 'cancel_pending', lambda **_kwargs: None)
    monkeypatch.setattr(context, 'get_space_context', lambda *_args, **_kwargs: {
        'peers': [], 'recent_discoveries': []})
    monkeypatch.setattr(social, 'activity_peers', lambda *_args: (None, []))
    monkeypatch.setattr('cloud.esp32.service.time.time', lambda: 1000)

    instance = ESP32Agent.__new__(ESP32Agent)
    instance._state = ESP32State()
    instance._conversation_lock = threading.Lock()
    instance._control_view_lock = threading.Lock()
    instance._peer_game_id = 'game'
    instance._autonomy_mode = 'reactive'
    instance._autonomy_epoch = 1
    instance._command_epoch = 1
    instance._user_hold_until = 0
    instance._autonomy_since = 0
    instance._last_reason_ts = 0
    instance._last_reason_sig = None
    instance._device_online = lambda: True
    instance._find_led_unit = lambda: 'esp32_desk_led'
    instance._sense = lambda: {
        'light_level': 'DARK', 'light_available': True,
        'sound_detected': False, 'time_period': '上午'}
    instance._social_state = SimpleNamespace(opportunity=lambda *_args, **_kwargs: {
        'kind': 'new_evidence', 'event_ids': ['new_peer_message']})
    instance._check_proactive = lambda _sense: ['unimproved']
    return instance


def test_confirmed_prize_keeps_reactive_and_social_but_timer_cannot_relight(agent):
    agent.end_peer_game('game', preserve_output=True)
    assert agent.get_autonomy_mode() == 'reactive'
    assert agent._should_act()
    assert agent._peer_game_id is None
    assert agent._last_act_ts == 0
    assert agent._stable_light_level == 'DARK'
    prepared = reactive.prepare_sensor_event(agent, {}, now=1100)
    assert prepared is not None
    assert prepared[0]['social_opportunity']
    assert prepared[0]['lighting_eligible'] is False
    # Own physical-state updates are not new environmental observations.
    own_state = reactive.prepare_sensor_event(agent, {
        'unit_id': 'esp32_desk_led', 'payload': {'ism': 'OFF'},
        'enqueue_ts': 1100, 'autonomy_epoch': agent._autonomy_epoch}, now=1100)
    assert own_state[0]['lighting_eligible'] is False


def test_new_activity_wakeup_bypasses_recent_sensor_throttle_without_enabling_light(agent, monkeypatch):
    from cloud.peers import social
    from cloud.peers.games import base

    agent._peer_game_id = None
    agent._check_proactive = lambda _sense: []
    agent._last_reason_ts = 1099
    activity = {'activity_id': 'act_new', 'started_at': 1100, 'context': '一起玩猜拳'}
    monkeypatch.setattr(social, 'activity_peers', lambda *_args: (activity, [{'unit_id': 'go2'}]))
    monkeypatch.setattr(base, 'game_invitation_block_reason', lambda *_args: 'lamp_result_unconfirmed')
    agent._social_state = social.SocialState()
    agent._social_state._born_at = 1000
    event = {'kind': 'activity_started', 'activity_id': 'act_new', 'enqueue_ts': 1100,
             'autonomy_epoch': agent._autonomy_epoch}
    # A genuine new request reaches the Graph; stale notifications cannot wake
    # an unrelated activity, and ordinary sensors keep their debounce.
    assert reactive.prepare_sensor_event(agent, {}, now=1100) is None
    assert reactive.prepare_sensor_event(agent, {**event, 'activity_id': 'act_old'}, now=1100) is None
    prepared = reactive.prepare_sensor_event(agent, event, now=1100)
    assert prepared[0]['social_opportunity']['kind'] == 'activity_request'
    assert prepared[0]['lighting_eligible'] is False
    assert prepared[0]['game_readiness']['go2'] == {
        'ready': False, 'reason': 'lamp_result_unconfirmed'}


@pytest.mark.parametrize('steps,status,consumed', [([], 'ok', True),
    ([], 'error', False), ([], 'blocked', False),
    ([{'tool': 'set_led_state', 'result': {'result': 'ok'}}], 'ok', False)])
def test_activity_request_is_consumed_after_silent_decision_but_not_before_social_judgment(
        agent, monkeypatch, steps, status, consumed):
    from cloud.peers.social import SocialState

    agent._peer_game_id = None
    agent._social_state = SocialState()
    now = agent._social_state._born_at + 1
    activity = {'activity_id': 'act_request', 'started_at': now, 'context': '一起玩猜拳'}
    opportunity = agent._social_state.opportunity(activity, [{'unit_id': 'go2'}], [], now=now)
    monkeypatch.setattr(reactive, 'prepare_sensor_event', lambda *_args, **_kwargs:
                        ({'social_opportunity': opportunity}, ('new_request',)))
    monkeypatch.setattr(reactive, 'run_reactive_event', lambda *_args, **_kwargs:
                        {'status': status, 'steps': steps})
    reactive.run_reactive_sensor_event(agent, {}, now=now)
    assert (opportunity['event_ids'][0] in agent._social_state.seen_discoveries) is consumed


def test_queued_old_sensor_events_cannot_release_prize_but_new_sensor_can(agent):
    previous_epoch = agent._autonomy_epoch
    agent.end_peer_game('game', preserve_output=True)
    event = {'unit_id': 'esp32_desk_light', 'payload': {'level': 'DARK', 'ts': 999},
             'enqueue_ts': 999, 'autonomy_epoch': previous_epoch}
    assert reactive.prepare_sensor_event(agent, event, now=1100) is None
    assert agent._game_output_preserved_since == 1000
    # Even a caller carrying the current epoch cannot revive an old input.
    event['autonomy_epoch'] = agent._autonomy_epoch
    assert reactive.prepare_sensor_event(agent, event, now=1100)[0]['lighting_eligible'] is False
    assert agent._game_output_preserved_since == 1000
    event['enqueue_ts'] = 1106
    event['payload']['ts'] = 1106
    prepared = reactive.prepare_sensor_event(agent, event, now=1106)
    assert prepared[0]['lighting_eligible'] is True
    assert agent._game_output_preserved_since is None


@pytest.mark.parametrize('timestamp', [0, 999, 1050])
def test_delayed_old_sound_payload_with_new_enqueue_cannot_release_or_load_training(
        agent, monkeypatch, timestamp):
    from cloud.cards import registry

    def forbidden():
        raise AssertionError('stale sound must not enter training lookup')

    monkeypatch.setattr(registry, 'get_registry', forbidden)
    agent.end_peer_game('game', preserve_output=True)
    prepared = reactive.prepare_sensor_event(agent, {
        'unit_id': 'esp32_desk_sound', 'payload': {'detected': True, 'ts': timestamp},
        'enqueue_ts': 1100, 'autonomy_epoch': agent._autonomy_epoch}, now=1100)
    assert prepared[0]['lighting_eligible'] is False
    assert not prepared[0].get('training_candidates')
    assert agent._game_output_preserved_since == 1000


def test_fresh_sound_event_releases_output_for_existing_reactive_policy(agent, monkeypatch):
    from cloud.cards import registry

    monkeypatch.setattr(registry, 'get_registry', lambda: SimpleNamespace(get_card=lambda _unit: None))
    agent.end_peer_game('game', preserve_output=True)
    prepared = reactive.prepare_sensor_event(agent, {
        'unit_id': 'esp32_desk_sound', 'payload': {'detected': True, 'ts': 1099},
        'enqueue_ts': 1100, 'autonomy_epoch': agent._autonomy_epoch}, now=1100)
    assert prepared[0]['lighting_eligible'] is True
    assert agent._game_output_preserved_since is None


def test_unrelated_game_release_cannot_claim_prize_or_change_mode(agent):
    agent.end_peer_game('another_game', preserve_output=True)
    assert agent._peer_game_id == 'game'
    assert not hasattr(agent, '_game_output_preserved_since')
    agent.set_autonomy_mode('manual')
    agent.end_peer_game('game', preserve_output=True)
    assert agent.get_autonomy_mode() == 'manual'
    assert agent._game_output_preserved_since is None


def test_user_takeover_is_not_delayed_by_game_prize(agent):
    agent.end_peer_game('game', preserve_output=True)
    agent.mark_user_command('esp32_desk_led')
    assert agent._game_output_preserved_since is None
    assert agent._user_hold_until == 1300
    assert not agent._should_act()


def test_new_sensor_event_still_needs_existing_material_change_and_settle(agent, monkeypatch):
    from cloud.agents.lamp.workflows.runtime import ReactiveMind

    clock = [1000]
    monkeypatch.setattr('cloud.esp32.service.time.time', lambda: clock[0])
    agent._check_proactive = lambda sense: ReactiveMind._check_proactive(agent, sense)
    observed = dict(agent._sense())
    agent._sense = lambda: dict(observed)
    agent.end_peer_game('game', preserve_output=True)
    clock[0] = 1100
    unchanged = reactive.prepare_sensor_event(agent, {
        'unit_id': 'esp32_desk_light', 'payload': {'level': 'DARK', 'ts': 1100},
        'enqueue_ts': 1100}, now=1100)
    assert unchanged[0]['lighting_eligible'] is False
    observed['light_level'] = 'DIM'
    clock[0] = 1106
    changing = reactive.prepare_sensor_event(agent, {
        'unit_id': 'esp32_desk_light', 'payload': {'level': 'DIM', 'ts': 1106},
        'enqueue_ts': 1106}, now=1106)
    assert changing[0]['lighting_eligible'] is False
    clock[0] = 1122
    settled = reactive.prepare_sensor_event(agent, {}, now=1122)
    assert settled[0]['lighting_eligible'] is True
    assert settled[0]['proactive_hints'] == ['env_changed']


def test_sensor_graph_keeps_preparation_epoch_across_game_finish(agent, monkeypatch):
    from langgraph.graph import END, START, StateGraph
    from langgraph.checkpoint.memory import InMemorySaver

    dispatched = []

    def prepare(instance, _event, **_kwargs):
        # Game completion races after the old observation was prepared.
        instance.end_peer_game('game', preserve_output=True)
        return {'lighting_eligible': True}, ('old_observation',)

    def build(instance, **_kwargs):
        def gate(state):
            if state['autonomy_epoch'] != instance._autonomy_epoch:
                return {'status': 'blocked'}
            dispatched.append('ON')
            return {'status': 'ok'}

        graph = StateGraph(reactive.ReactiveState)
        graph.add_node('gate', gate)
        graph.add_edge(START, 'gate')
        graph.add_edge('gate', END)
        return graph.compile(checkpointer=InMemorySaver())

    monkeypatch.setattr(reactive, 'prepare_sensor_event', prepare)
    monkeypatch.setattr(reactive, 'build_reactive_graph', build)
    result = reactive.run_reactive_sensor_event(agent, {}, checkpointer=InMemorySaver(), now=1100)
    assert result['status'] == 'blocked'
    assert not dispatched

