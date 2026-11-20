"""Cookie and CSRF boundaries for the parameter-only rule editor."""
import pytest

from cloud.api.training import router
from cloud.api.tests.conftest import login_admin
from cloud.agents.common.training.parameter_edit_checks import editing  # noqa: F401


@pytest.mark.asyncio
async def test_parameter_edit_requires_session_csrf_and_preserves_rule_identity(client, app, editing):
    app.include_router(router)
    record = editing.blink
    url = f"/api/training/{record['teaching_id']}/parameters"
    payload = {'expected_revision': 1, 'capability_hash': record['capability_hash'],
               'steps': [{'step_index': 0, 'params': {'count': 3}}]}
    assert (await client.patch(url, json=payload)).status_code == 401
    headers = await login_admin(client)
    assert (await client.patch(url, json=payload)).status_code == 403
    response = await client.patch(url, json=payload, headers=headers)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['teaching_id'] == record['teaching_id'] and saved['revision'] == 2
    assert saved['definition']['steps'][0]['defaults'] == {'count': 3}
    assert (await client.patch(url, json=payload, headers=headers)).status_code == 409
    invalid = {**payload, 'expected_revision': 2, 'action': 'SET_STATE'}
    assert (await client.patch(url, json=invalid, headers=headers)).status_code == 422
