"""Scoped contact motion for an observed cargo, using the existing Move tool.

Only the cargo executor enters this scope. It is not a model/browser parameter,
does not edit the shared map, and cannot outlive its original device session.
"""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass

from cloud.go2.connection import go2
from cloud.go2.control import control_lease
from cloud.observability import emit_agent_event


_contact = ContextVar('go2_contact_push', default=None)


@dataclass
class ContactMotion:
    context: object
    connection: object
    odom_session: str
    active: bool = True

    def check(self):
        from cloud.go2.tools.executor import validate_execution_context
        validate_execution_context(self.context)
        owner = control_lease.snapshot()
        if (not self.active or not go2.is_connected or go2._conn is not self.connection
                or go2._odom_session_id != self.odom_session
                or owner is None or owner.owner_session_id != self.context.session_id):
            raise ValueError('contact_push_ownership_changed')


def current_contact_motion(context, direction):
    scope = _contact.get()
    if scope is None:
        return None
    if (direction != 'forward' or context.session_id != scope.context.session_id
            or context.goal_revision != scope.context.goal_revision):
        raise ValueError('contact_push_requires_original_forward_motion')
    scope.check()
    return scope


async def execute_contact_push(context, operation):
    """Disable avoidance for one forward operation, stop, then restore it.

    The operation retains the normal tool's training, ledger and physical receipts.
    Cancellation waits for cleanup; late cleanup never writes a replacement device
    connection. A timed-out SET may have changed the robot, so restoration is
    attempted even when disabling never returns a successful response.
    """
    from cloud.go2.navigation.navigator import navigator
    from cloud.go2.tools.executor import validate_execution_context

    validate_execution_context(context)
    control_lease.heartbeat(context)
    scope = ContactMotion(context, go2._conn, go2._odom_session_id)
    scope.check()
    expected = getattr(go2, '_avoidance_restore_expected', None)
    if expected is not None:
        await go2.set_avoidance_switch(expected, connection=scope.connection)
        go2._avoidance_restore_expected = None
    before = await go2.read_avoidance_switch()
    scope.check()
    if before.get('result') != 'ok' or type(before.get('enabled')) is not bool:
        return {'result': 'blocked', 'reason': 'avoidance_switch_unknown',
                'message': '未能读取原避障设置，本次推送未开始。'}
    report = {'before': before['enabled'], 'disabled': False, 'restored': False}
    movement, changed, token = None, False, None
    try:
        if before['enabled']:
            changed = True
            await go2.set_avoidance_switch(False, connection=scope.connection)
        scope.check()
        report['disabled'] = True
        token = _contact.set(scope)
        emit_agent_event('navigation.progress', phase='contact_push_started', avoidance=report.copy())
        movement = await operation()
    except (ValueError, RuntimeError, TimeoutError) as exc:
        movement = {'result': 'blocked', 'reason': str(exc) or type(exc).__name__,
                    'message': '本次接触推送已停止，保留避障切换与运动的实际结果。'}
    finally:
        scope.active = False
        if token is not None:
            _contact.reset(token)

        async def cleanup():
            same_connection = go2.is_connected and go2._conn is scope.connection
            owner = control_lease.snapshot()
            if same_connection and owner and owner.owner_session_id == context.session_id:
                if movement is None or (movement.get('stop') or {}).get('result') != 'ok':
                    try:
                        report['stop'] = await navigator.stop_and_confirm(owner_session_id=context.session_id)
                    except (ValueError, RuntimeError, TimeoutError) as exc:
                        report['stop'] = {'result': 'accepted', 'reason': 'stop_unconfirmed',
                                          'error': str(exc) or type(exc).__name__}
            if changed and same_connection:
                try:
                    await go2.set_avoidance_switch(before['enabled'], connection=scope.connection)
                    report['restored'] = True
                    go2._avoidance_restore_expected = None
                except (ValueError, RuntimeError, TimeoutError) as exc:
                    report['restore_error'] = str(exc) or type(exc).__name__
                    go2._avoidance_restore_expected = before['enabled']
            elif not changed:
                report['restored'] = True
            else:
                report['restore_error'] = 'avoidance_connection_changed'
                go2._avoidance_restore_expected = before['enabled']
            stop = (movement or {}).get('stop') or report.get('stop') or {}
            if not report['restored'] or stop.get('result') != 'ok':
                owner = control_lease.snapshot()
                if owner and owner.owner_session_id == context.session_id:
                    control_lease.hold_for_handoff(owner)
            emit_agent_event('navigation.progress', phase='contact_push_finished', avoidance=report.copy())

        cleanup_task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await cleanup_task
            raise
    movement = {**movement, 'avoidance': report}
    if not movement.get('stop') and report.get('stop'):
        movement['stop'] = report['stop']
    if not report['restored']:
        return {**movement, 'result': 'accepted', 'reason': 'avoidance_restore_unconfirmed',
                'message': '本次运动回执已保留，但原避障设置尚未确认恢复。'}
    if (movement.get('stop') or {}).get('result') != 'ok':
        return {**movement, 'result': 'accepted', 'reason': 'stop_unconfirmed'}
    return movement
