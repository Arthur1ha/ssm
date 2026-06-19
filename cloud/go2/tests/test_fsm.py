"""Go2FSM 状态模型：动作即状态、可打断、进度事件返回、无 12s 定时器。"""
import cloud.go2.connection.fsm as fsm_mod
from cloud.go2.connection.fsm import Go2FSM, _FSM_AVAILABLE, _TRANSIENT_STATES


def test_state_set_has_action_states_no_executing():
    assert "executing" not in _FSM_AVAILABLE
    for s in ("greeting", "stretching", "dancing1", "dancing2"):
        assert s in _FSM_AVAILABLE
    assert _TRANSIENT_STATES == {"greeting", "stretching", "dancing1", "dancing2"}


def test_gesture_transitions_from_standing():
    fsm = Go2FSM()
    fsm.fsm_state = "standing"
    assert fsm.fsm_next("Hello") == "greeting"
    assert fsm.fsm_next("Stretch") == "stretching"
    assert fsm.fsm_next("Dance1") == "dancing1"
    assert fsm.fsm_next("Dance2") == "dancing2"
    assert fsm.fsm_next("Move") == "moving"
    assert fsm.fsm_next("StandDown") == "lying"


def test_action_state_is_interruptible():
    fsm = Go2FSM()
    fsm.fsm_state = "greeting"
    assert fsm.fsm_next("Dance2") == "dancing2"     # 打断切到另一个动作
    assert fsm.fsm_next("Move") == "moving"
    assert fsm.fsm_next("StopMove") == "standing"


def test_moving_has_no_self_move():
    fsm = Go2FSM()
    fsm.fsm_state = "moving"
    assert fsm.fsm_next("Move") is None             # moving 不自环（与现状一致）
    assert fsm.fsm_next("StopMove") == "standing"
    assert fsm.fsm_next("Hello") == "greeting"


def test_progress_done_returns_transient_to_standing():
    fsm = Go2FSM()
    fsm.fsm_state = "greeting"
    fsm._fsm_on_progress(0.6)   # 在动
    fsm._fsm_on_progress(0.0)   # 停了 = 做完
    assert fsm.fsm_state == "standing"


def test_progress_does_not_reset_stable_state():
    fsm = Go2FSM()
    fsm.fsm_state = "moving"
    fsm._fsm_on_progress(0.6)
    fsm._fsm_on_progress(0.0)
    assert fsm.fsm_state == "moving"


def test_reset_timer_removed():
    assert not hasattr(fsm_mod, "_EXEC_RESET_DELAY")
    assert not hasattr(Go2FSM, "_schedule_exec_reset")
