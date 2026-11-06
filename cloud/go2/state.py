"""Go2 facts and constrained life transitions. No planning or hardware I/O here."""
from copy import deepcopy
import time
import uuid
import threading
from functools import wraps

from cloud.go2.actions import BODY_STATES


def synchronized(fn):
    @wraps(fn)
    def run(self, *args, **kwargs):
        outer = getattr(self._local, 'depth', 0) == 0
        try:
            with self._lock:
                self._local.depth = getattr(self._local, 'depth', 0) + 1
                try:
                    return fn(self, *args, **kwargs)
                finally:
                    self._local.depth -= 1
        finally:
            if outer and getattr(self._local, 'changed', False):
                self._local.changed = False
                if self._notify:
                    self._notify()
    return run


STABLE_BODY_STATES = frozenset({'standing', 'lying', 'sitting'})
LIFE_ACTIONS = ('request_rest', 'wake', 'start_explore', 'stop_explore')


class Go2State:
    def __init__(self, notify=None):
        self._lock = threading.RLock()
        self._local = threading.local()
        self._notify = notify
        self.data = dict(state_schema_version=2, epoch=uuid.uuid4().hex, revision=0,
                         connected=False, connection_state='offline', activity_state=None,
                         life_state=None, body_state=None, body_confirmation='unknown',
                         body_observed_at=None, last_confirmed_body=None, body_telemetry_fresh=False,
                         body_observation_source=None, pending_action=None, last_action=None,
                         control_mode='normal', autonomy_mode='manual',
                         navigation_active=False, velocity_requested=False,
                         life_owner=None, life_task_id=None, life_finish_requested=False,
                         exploration_enabled=False, last_transition=None)

    @property
    def exploration_enabled(self):
        return self.data['exploration_enabled']

    @exploration_enabled.setter
    def exploration_enabled(self, value):
        self.commit('exploration_intent', exploration_enabled=bool(value))

    @synchronized
    def commit(self, event, **changes):
        if not any(self.data.get(k) != v for k, v in changes.items()):
            return
        self.data.update(deepcopy(changes))
        self.data['revision'] += 1
        self.data['last_transition'] = dict(event=event, at=time.time(),
                                            task_id=self.data['life_task_id'])
        self._local.changed = True

    @synchronized
    def connection(self, value):
        if value == self.data['connection_state']:
            return
        changes = dict(connection_state=value, connected=value == 'online',
                       body_state=None, body_confirmation='unknown', body_observation_source=None,
                       pending_action=None, life_finish_requested=False,
                       body_observed_at=None, body_telemetry_fresh=False, navigation_active=False, velocity_requested=False, control_mode='normal', life_owner=None, life_task_id=None,
                       exploration_enabled=False, activity_state='active' if value == 'online' else None,
                       life_state='idle' if value == 'online' else None)
        if value == 'connecting':
            changes.update(epoch=uuid.uuid4().hex, revision=-1)
        self.commit('connection_' + value, **changes)

    @synchronized
    def body(self, value, *, confirmation=None, source=None):
        if value is not None and value not in BODY_STATES:
            raise ValueError('invalid body state')
        changes = dict(body_state=value)
        if confirmation is not None:
            changes['body_confirmation'] = confirmation
        if source is not None:
            changes['body_observation_source'] = source
        if (confirmation in {'observed', 'previous_observation'} and value in STABLE_BODY_STATES
                and (self.data['body_state'] != value or self.data['body_confirmation'] != confirmation)):
            changes.update(last_confirmed_body=value, body_observed_at=time.time(), body_telemetry_fresh=True)
        self.commit('body_observation' if confirmation == 'observed' else 'body_changed', **changes)

    @synchronized
    def active_reason(self):
        if self.data['connection_state'] != 'online':
            return 'Go2 未连接'
        if self.data['activity_state'] == 'rest':
            return '机器狗正在休息，请先唤醒'
        return None

    @synchronized
    def life_reasons(self, *, navigation_busy=False):
        d = self.data
        active_reason = self.active_reason()
        stable = d['body_state'] in STABLE_BODY_STATES and d['body_confirmation'] in {'observed', 'previous_observation'}
        return {
            'request_rest': active_reason or ('生活状态忙碌' if d['life_state'] != 'idle' else
                            '等待有效设备反馈' if not stable else
                            '身体动作待确认' if d['pending_action'] else
                            '运动任务未结束' if navigation_busy or d['navigation_active'] or d['velocity_requested'] else
                            '请先退出遥控' if d['control_mode'] == 'remote' else None),
            'wake': 'Go2 未连接' if not d['connected'] else None if d['activity_state'] == 'rest' else '当前未休息',
            'start_explore': active_reason or ('等待有效设备反馈' if not stable else
                             '需要已确认站立' if d['body_state'] != 'standing' else
                             '身体动作待确认' if d['pending_action'] else
                             '已有生活任务' if d['life_state'] != 'idle' else None),
            'stop_explore': 'Go2 未连接' if not d['connected'] else
                            None if d['life_state'] == 'explore' or (self.exploration_enabled and d['life_state'] == 'idle') else '当前未探索',
        }

    @synchronized
    def request_rest(self, *, navigation_busy=False):
        reason = self.life_reasons(navigation_busy=navigation_busy)['request_rest']
        if reason:
            raise ValueError(reason)
        self.commit('request_rest', exploration_enabled=False, activity_state='rest', life_state=None,
                    life_owner=None, life_task_id=None)

    @synchronized
    def wake(self):
        reason = self.life_reasons()['wake']
        if reason:
            raise ValueError(reason)
        self.commit('wake', exploration_enabled=False, activity_state='active', life_state='idle', life_owner=None, life_task_id=None)

    @synchronized
    def begin_life(self, state, owner, task_id):
        reason = self.active_reason()
        if reason:
            raise ValueError(reason)
        if state not in {'play', 'explore'}:
            raise ValueError('invalid life state')
        self.commit('task_accepted', life_state=state, life_owner=owner, life_task_id=task_id,
                    life_finish_requested=False)
        return self.data['epoch'], task_id

    @synchronized
    def finish_life(self, token):
        if token != (self.data['epoch'], self.data['life_task_id']):
            return False
        pending = self.data['pending_action']
        if pending and pending.get('life_task_id') == self.data['life_task_id']:
            # A transport response or completed model turn does not end the
            # physical action. Its observation callback releases this task.
            self.commit('task_waiting_for_body', life_finish_requested=True)
            return False
        self.commit('task_finished', life_state='idle' if self.data['activity_state'] == 'active' else None,
                    life_owner=None, life_task_id=None, life_finish_requested=False)
        return True

    @synchronized
    def finish_body_action(self, request_id, result, *, phase, confirmation=None, error=None):
        pending = self.data['pending_action']
        if not pending or pending.get('request_id') != request_id:
            return False
        last = {**pending, 'phase': phase, 'result': result, 'finished_at': time.time(), 'ts': time.time()}
        if confirmation:
            last['confirmation'] = confirmation
        if error:
            last['error'] = error
        token = (self.data['epoch'], pending.get('life_task_id'))
        release_life = self.data['life_finish_requested']
        self.commit('body_action_' + phase, pending_action=None, last_action=last)
        if release_life:
            self.finish_life(token)
        return True

    @synchronized
    def snapshot(self):
        d = deepcopy(self.data)
        d['fsm_state'] = d['body_state'] if d['connected'] else d['connection_state']
        return d


def runtime_for(robot):
    """Return the fact store only for adapters implementing the v2 contract."""
    runtime = getattr(robot, 'runtime', None)
    return runtime if isinstance(runtime, Go2State) else None
