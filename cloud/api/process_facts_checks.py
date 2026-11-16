"""Same bounded original facts across trusted MQTT, SSE and activity history."""
import json
from types import SimpleNamespace

import pytest

from cloud.api import intents
from cloud.space.activity import ActivityStore


@pytest.mark.parametrize('agent_id,device_id', [('go2', 'go2'), ('esp32_desk_led', 'esp32_desk')])
def test_delegated_mqtt_reply_keeps_speaker_emotion_and_source_identity(monkeypatch, agent_id, device_id):
    broker = intents._EventBroker()
    monkeypatch.setattr(intents, '_events', broker)
    monkeypatch.setattr(intents, 'get_registry', lambda: SimpleNamespace(
        get_card=lambda unit: {'unit_id': unit, 'device_id': device_id}))
    monkeypatch.setattr(intents, 'get_space_registry', lambda: SimpleNamespace(
        get_space=lambda: {'device_ids': [device_id]}))
    channel = broker.subscribe('chat-real')
    payload = dict(type='agent_reply', agent_id=agent_id, source='delegated_skill', status='ok',
        run_id='child-one', parent_run_id='parent-one', event_id='reply-one', source_event_id='source-one',
        trace_id='trace-one', ts=1234, text='做好啦。', emotion='happy', reply_kind='final',
        context={'secret': 'must not escape'}, scope='space', space_id='elsewhere')
    assert intents.handle_mqtt_message('ssm/feedback/chat-real', json.dumps(payload))
    event = channel.get_nowait()
    assert event['agent_id'] == agent_id
    assert event['emotion'] == 'happy'
    assert event['source'] == 'delegated_skill'
    assert event['parent_run_id'] == 'parent-one'
    assert event['event_id'] == 'reply-one'
    assert event['source_event_id'] == 'source-one'
    assert event['ts'] == 1234
    assert event['scope'] == 'session' and event['session_id'] == 'chat-real'
    assert event['space_id'] == 'main'
    assert 'context' not in event
    intents.handle_mqtt_message('ssm/feedback/chat-real', json.dumps(payload))
    assert channel.empty()


def test_mqtt_lifecycle_keeps_question_and_runtime_type_without_persona_text(monkeypatch):
    broker = intents._EventBroker()
    monkeypatch.setattr(intents, '_events', broker)
    channel = broker.subscribe('chat-real')
    intents.handle_mqtt_message('ssm/feedback/chat-real', json.dumps(dict(
        type='run_stage', agent_id='cloud_orchestrator', run_id='run-one', status='needs_clarification',
        stage='question', question='哪盏灯？', reply_source='runtime', ts=2000, sequence=7)))
    event = channel.get_nowait()
    assert event['type'] == 'run_stage'
    assert event['stage'] == 'question'
    assert event['question'] == '哪盏灯？'
    assert event['reply_source'] == 'runtime'
    assert event['ts'] == 2000 and event['sequence'] == 7


def test_original_observation_candidate_and_rules_survive_sse_history(monkeypatch, tmp_path):
    store = ActivityStore(tmp_path / 'activity.json')
    revision = store.note_user_intervention('request-one')
    activity_id = store.begin('一起找东西', 'request-one', revision)['activity_id']
    monkeypatch.setattr('cloud.space.activity.get_activity_store', lambda: store)
    monkeypatch.setattr('cloud.go2.router.get_activity_mode_owner', lambda: activity_id)
    broker = intents._EventBroker()
    monkeypatch.setattr(intents, '_events', broker)
    channel = broker.subscribe('viewer')
    observation = {'found': None, 'frame_id': 'frame-one', 'as_of': 900,
        'scene_summary': '前方有纸箱。', 'proximity': 'uncertain', 'prompt': 'private'}
    first = intents.publish_agent_event('agent_progress', agent_id='go2', scope='space', source='object_search',
        status='executing', run_id='search-one', task_id='search-one', activity_id=activity_id,
        stage='search_observe', sequence=1, ts=1000, fact_kind='observation', result_kind='query',
        observation=observation)
    second = intents.publish_agent_event('agent_progress', agent_id='go2', scope='space', source='object_search',
        status='executing', run_id='search-one', task_id='search-one', activity_id=activity_id,
        stage='search_selected', sequence=2, ts=1001, fact_kind='decision', result_kind='query',
        public_result={'candidate': {'source': 'frontier', 'x': 1, 'y': 2, 'context': 'private'},
            'game_rules': {'rule_id': 'rps_siege', 'status': 'proposed',
                          'mapping': [{'choice': 'rock', 'label': '红色'}]}, 'context': 'private'})
    rows = store.events(activity_id)[-2:]
    assert first['ts'] == rows[0]['ts'] == 1000
    assert rows[0]['observation'] == first['observation']
    assert rows[0]['observation']['found'] is None
    assert rows[0]['observation']['as_of'] == 900
    assert 'prompt' not in rows[0]['observation']
    assert rows[1]['public_result'] == second['public_result']
    assert rows[1]['public_result']['candidate'] == {'source': 'frontier', 'x': 1, 'y': 2}
    assert rows[1]['public_result']['game_rules']['mapping'] == [{'choice': 'rock', 'label': '红色'}]
    assert [row['source_event_id'] for row in rows] == [first['event_id'], second['event_id']]
    assert [channel.get_nowait()['event_id'] for _ in range(2)] == [first['event_id'], second['event_id']]
    assert ActivityStore(tmp_path / 'activity.json').events(activity_id)[-2:] == rows
