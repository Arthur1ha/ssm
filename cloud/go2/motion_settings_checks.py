"""Native settings are read-only evidence, never physical gait confirmation."""

import json

import pytest

from cloud.go2.move_control_checks import robot_at_stand


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
    {'gait': 0, 'economicGait': False},
    json.dumps({'gait': 3, 'continuousGait': True}),
    {}, '', 'invalid', None,
])
async def test_read_settings_preserves_native_values_without_selecting_gait(payload):
    robot, rpc, packets = robot_at_stand()
    robot._classic_walk_connection = None
    before = robot.state_snapshot()
    rpc.return_value = {'data': {'header': {'status': {'code': 0}}, 'data': payload}}
    result = await robot.read_motion_settings()
    expected = json.loads(payload) if isinstance(payload, str) and payload.startswith('{') else payload
    valid = isinstance(expected, dict) and bool(expected)
    assert result['result'] == ('ok' if valid else 'unknown')
    assert result['settings'] == (expected if valid else None)
    assert result['confirmation_scope'] == 'settings_only'
    options = rpc.await_args.args[1]
    assert rpc.await_count == 1 and options['api_id'] == 1034
    assert options['id'] > 0 and 'gait' in options['parameter']
    assert robot.state_snapshot() == before
    assert robot._classic_walk_connection is None
    packets.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [None, False, -1, 3203])
async def test_rejected_or_missing_settings_reply_is_unknown(code):
    robot, rpc, packets = robot_at_stand()
    rpc.return_value = {'data': {'header': {'status': {'code': code}}, 'data': {'gait': 1}}}
    assert (await robot.read_motion_settings())['settings'] is None
    packets.assert_not_called()


@pytest.mark.asyncio
async def test_settings_from_old_connection_cannot_describe_new_connection():
    robot, rpc, packets = robot_at_stand()

    async def replaced(*_):
        robot._conn = object()
        return {'data': {'header': {'status': {'code': 0}}, 'data': {'gait': 1}}}

    rpc.side_effect = replaced
    with pytest.raises(ValueError, match='connection_changed'):
        await robot.read_motion_settings()
    packets.assert_not_called()
