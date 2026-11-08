"""Retirement checks use isolated files and never start the service lifespan."""
import json
import pytest
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[4]
RETIRED = ('cloud/api/teachings.py', 'cloud/agents/common/teachings.py',
           'cloud/agents/common/teaching_bindings.py', 'app/src/pages/RulesPage.jsx',
           'app/src/components/RulesDrawer.jsx')
LEGACY = re.compile(r'save_rule|pending_rule|onPendingRule|teaching_candidates|teaching_bindings|go2_list_teachings|teachings_endpoint|/api/teachings|RulesPage|RulesDrawer|autonomous_skills|from cloud\.agents\.common import teachings')


def test_retired_sources_and_production_references():
    assert not [name for name in RETIRED if (ROOT / name).exists()]
    found = []
    for base in ('cloud', 'app'):
        for path in (ROOT / base).rglob('*'):
            if path.suffix not in {'.py', '.js', '.jsx', '.html'} or path.name.endswith('_checks.py'):
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if LEGACY.search(line):
                    found.append(f'{path.relative_to(ROOT)}:{number}')
    assert not found, '\n'.join(found)


def test_old_enabled_data_is_inert(monkeypatch, tmp_path):
    from cloud.agents.common.training.store import list_records
    from cloud.agents.common.training.policy import training_event_intent
    from cloud.agents.common.training.check_fixtures import hello_cooldown
    from cloud.agents.common.training.catalog import catalog_for
    from cloud.go2.card import _build_go2_card
    legacy = tmp_path / 'old'
    legacy.mkdir()
    path = legacy / 'go2.json'
    original = json.dumps([hello_cooldown(60) | {'execution_status': 'enabled'}])
    path.write_text(original)
    monkeypatch.setenv('SSM_TEACHINGS_DIR', str(legacy))
    monkeypatch.setenv('SSM_TRAINING_DIR', str(tmp_path / 'new'))
    records = list_records('go2', space_id='main')
    assert records == []
    assert training_event_intent({'id': 'person_detected', 'event_id': 'e1', 'ts': 100},
        records, catalog_for(_build_go2_card()), {'state_fresh': True}, now=100) is None
    assert path.read_text() == original


@pytest.mark.asyncio
async def test_full_app_old_routes_are_404(monkeypatch, tmp_path):
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED', '1')
    for key in ('SSM_TRAINING_DIR', 'SSM_TEACHINGS_DIR', 'SSM_EFFECT_LEDGER_DIR',
                'SSM_AGENT_DATA_DIR', 'SSM_CHECKPOINT_DIR'):
        monkeypatch.setenv(key, str(tmp_path / key))
    # Existing registry defaults to a production file; substitute it before assembly.
    from cloud.space import registry
    monkeypatch.setattr(registry, '_registry', registry.SpaceRegistry(tmp_path / 'space.json'))
    from cloud.api.main import app
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for method, path in [('GET', '/api/teachings'), ('POST', '/api/teachings'),
                             ('PUT', '/api/teachings/old/binding'),
                             ('DELETE', '/api/teachings/old/binding')]:
            assert (await client.request(method, path, json={})).status_code == 404
