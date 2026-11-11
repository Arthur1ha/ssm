"""Go2 facts and constrained life transitions. No planning or hardware I/O here."""
from copy import deepcopy
from dataclasses import dataclass
import time
import uuid
import threading
from functools import wraps

from cloud.go2.actions import BODY_STATES, STABLE_BODY_STATES


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


@dataclass(frozen=True)
class LifeActionSpec:
    action: str
    label: str
    region: str
    sources: tuple[str, ...]
    target: str
    guards: tuple[str, ...]
    required_actor: str | None = None


# Guard order is also the rejection priority. Card explains these same guards;
# runtime evaluates them against current facts immediately before execution.
LIFE_GUARDS = {
    "connected": "Go2 未连接",
    "active": "机器狗正在休息，请先唤醒",
    "idle": "生活状态忙碌",
    "no_life_task": "生活状态忙碌",
    "stable": "等待有效设备反馈",
    "standing": "需要已确认站立",
    "no_body_action": "身体动作待确认",
    "no_motion": "运动任务未结束",
    "not_remote": "请先退出遥控",
    "exploring": "当前未探索",
}
STATE_REASON_LABELS = {
    "body_telemetry_stale": "设备反馈已过期，等待最新状态",
    "body_action_unconfirmed": "上个动作的结果尚未确认",
    "body_action_pending_confirmation": "上个动作的结果尚未确认",
    "exploration_owned_by_another_session": "探索由另一位用户控制",
    "unknown_posture": "等待有效设备反馈",
    "navigation_unavailable": "当前定位或地图不可用",
}
LIFE_ACTION_SPECS = {spec.action: spec for spec in (
    LifeActionSpec("request_rest", "休息", "activity", ("active",), "rest",
                   ("connected", "active", "no_life_task", "stable", "no_body_action", "no_motion", "not_remote")),
    LifeActionSpec("wake", "唤醒", "activity", ("rest",), "active", ("connected",)),
    LifeActionSpec("start_explore", "开启探索", "life", ("idle",), "explore",
                   ("connected", "active", "stable", "standing", "no_body_action", "idle"), "admin"),
    LifeActionSpec("stop_explore", "停止探索", "life", ("explore", "idle"), "idle",
                   ("connected", "exploring")),
)}
LIFE_ACTIONS = tuple(LIFE_ACTION_SPECS)
LIFE_VIEWS = {
    "activity": {
        "active": {"label": "可活动", "description": "生活与身体区域同时有效。", "type": "generic"},
        "rest": {"label": "休息", "description": "保持连接与当前稳定姿态，暂停自主活动。", "type": "generic"},
    },
    "life": {
        "idle": {"label": "待命", "description": "等待用户或现有自主策略接纳任务。", "type": "generic"},
        "play": {"label": "互动", "description": "已接纳的互动任务正在进行。", "type": "generic"},
        "explore": {"label": "探索", "description": "自由探索正在运行，观察、规划和轮次间等待均保持探索。", "type": "generic"},
    },
}
LIFE_TASK_STATES = ("play", "explore")


def life_tool_schema():
    return {"type": "object", "properties": {"action": {
        "type": "string", "enum": list(LIFE_ACTION_SPECS),
        "x-enum-labels": {key: spec.label for key, spec in LIFE_ACTION_SPECS.items()},
    }}, "required": ["action"], "additionalProperties": False}


def life_transitions(region):
    transitions = []
    for spec in LIFE_ACTION_SPECS.values():
        if spec.region != region:
            continue
        for source in spec.sources:
            # Source and parent states are already visible on the diagram.
            # Keep the remaining guards beside the trigger that needs them.
            guard_labels = {"stable": "姿态已确认", "standing": "站立已确认",
                            "no_body_action": "无待确认动作", "no_motion": "无运动任务",
                            "not_remote": "非遥控", "exploring": "探索已开启", "idle": "待命",
                            "no_life_task": "无进行中的任务"}
            rule_conditions = ["收到" + spec.label + "指令"]
            if spec.required_actor == "admin":
                rule_conditions[0] = "收到管理员的" + spec.label + "指令"
            for guard in spec.guards:
                if guard not in guard_labels or guard == "idle" and source == "idle":
                    continue
                if guard == "stable" and "standing" in spec.guards:
                    continue
                rule_conditions.append(guard_labels[guard])
            transitions.append({
                "src": source, "dst": spec.target, "trigger": spec.action,
                "label": spec.label, "action": "LIFE", "params": {"action": spec.action},
                "training_fixed_params": ["action"],
                **({"training_disabled": True} if spec.action in {"start_explore", "stop_explore"} else {}),
                "availability_key": spec.action,
                "trigger_condition": "，且".join(rule_conditions),
                **({"required_actor": spec.required_actor} if spec.required_actor else {}),
                **({"available_only": True} if source == spec.target else {}),
            })
    if region == "life":
        transitions.extend([
            {"src": "idle", "dst": "explore", "trigger": "exploration_started", "label": "自主探索启动",
             "trigger_condition": "自由探索已启动，且当前可活动、无互动任务"},
            {"src": "explore", "dst": "idle", "trigger": "exploration_stopped", "label": "自主探索停止",
             "trigger_condition": "探索许可已撤销，且无待确认的探索任务动作"},
            {"src": "play", "dst": "idle", "trigger": "task_finished", "label": "互动结束",
             "trigger_condition": "互动任务结束，且无待确认的任务动作、无有效探索许可"},
            {"src": "play", "dst": "explore", "trigger": "task_finished", "label": "恢复探索",
             "trigger_condition": "互动任务结束，且无待确认的任务动作、探索许可仍有效"},
        ])
        for source in ("idle", "explore"):
            transitions.append({"src": source, "dst": "play", "trigger": "task_accepted", "label": "接到互动任务",
                                "trigger_condition": "互动任务已接纳"})
    return transitions


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
                         exploration_enabled=False, exploration_owner=None, last_transition=None)

    @property
    def exploration_enabled(self):
        return self.data['exploration_enabled']

    @exploration_enabled.setter
    def exploration_enabled(self, value):
        self.set_exploration(value, self.data['exploration_owner'])

    @property
    def life_task_available(self):
        """An exploration strategy between segments can yield to a new task."""
        with self._lock:
            return self.data['life_state'] == 'idle' or (
                self.data['life_state'] == 'explore' and self.data['life_task_id'] is None)

    @synchronized
    def set_exploration(self, enabled, owner=None):
        enabled = bool(enabled)
        changes = dict(exploration_enabled=enabled, exploration_owner=owner if enabled else None)
        if not self.active_reason() and self.life_task_available:
            changes.update(life_state='explore' if enabled else 'idle',
                           life_owner=owner if enabled else None)
        self.commit('exploration_started' if enabled else 'exploration_stopped', **changes)

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
                       autonomy_mode='manual', exploration_enabled=False, exploration_owner=None,
                       activity_state='active' if value == 'online' else None,
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
        conditions = {
            "connected": d["connected"], "active": not active_reason,
            "idle": d["life_state"] == "idle", "no_life_task": self.life_task_available, "stable": stable,
            "standing": d["body_state"] == "standing",
            "no_body_action": not d["pending_action"],
            "no_motion": not (navigation_busy or d["navigation_active"] or d["velocity_requested"]),
            "not_remote": d["control_mode"] != "remote",
            "exploring": d["life_state"] == "explore" or (self.exploration_enabled and d["life_state"] == "idle"),
        }
        reasons = {}
        for action, spec in LIFE_ACTION_SPECS.items():
            reason = next((LIFE_GUARDS[key] for key in spec.guards if not conditions[key]), None)
            if not reason and d[spec.region + "_state"] not in spec.sources:
                reason = "当前未休息" if action == "wake" else "当前行为不允许此请求"
            reasons[action] = reason
        return reasons

    @synchronized
    def request_rest(self, *, navigation_busy=False):
        reason = self.life_reasons(navigation_busy=navigation_busy)['request_rest']
        if reason:
            raise ValueError(reason)
        self.commit('request_rest', exploration_enabled=False, exploration_owner=None,
                    activity_state=LIFE_ACTION_SPECS['request_rest'].target, life_state=None,
                    life_owner=None, life_task_id=None)

    @synchronized
    def wake(self):
        reason = self.life_reasons()['wake']
        if reason:
            raise ValueError(reason)
        self.commit('wake', exploration_enabled=False, exploration_owner=None,
                    activity_state=LIFE_ACTION_SPECS['wake'].target, life_state='idle', life_owner=None, life_task_id=None)

    @synchronized
    def begin_life(self, state, owner, task_id):
        reason = self.active_reason()
        if reason:
            raise ValueError(reason)
        if state not in LIFE_TASK_STATES:
            raise ValueError('invalid life state')
        self.commit('task_accepted', life_state=state, life_owner=owner, life_task_id=task_id,
                    life_finish_requested=False)
        return self.data['epoch'], task_id

    @synchronized
    def finish_life(self, token):
        if self.data['life_task_id'] is None or token != (self.data['epoch'], self.data['life_task_id']):
            return False
        pending = self.data['pending_action']
        if pending and pending.get('life_task_id') == self.data['life_task_id']:
            # A transport response or completed model turn does not end the
            # physical action. Its observation callback releases this task.
            self.commit('task_waiting_for_body', life_finish_requested=True)
            return False
        active = self.data['activity_state'] == 'active'
        exploring = active and self.exploration_enabled
        self.commit('task_finished', life_state=('explore' if exploring else 'idle') if active else None,
                    life_owner=self.data['exploration_owner'] if exploring else None,
                    life_task_id=None, life_finish_requested=False)
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
        return deepcopy(self.data)


def runtime_for(robot):
    """Return the fact store only for adapters implementing the v2 contract."""
    runtime = getattr(robot, 'runtime', None)
    return runtime if isinstance(runtime, Go2State) else None
