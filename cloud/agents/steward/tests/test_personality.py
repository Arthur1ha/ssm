"""The migrated steward reads the administrator's existing profile file."""

from cloud.agents.steward.personality import profile as migrated
from cloud.agents.steward.personality import profile as api_profile


def test_shared_profile_path_and_immediate_admin_edit(monkeypatch, tmp_path):
    assert migrated.PROFILE_FILE == api_profile.PROFILE_FILE
    isolated = tmp_path / "profile.json"
    monkeypatch.setattr(migrated._store, "path", isolated)
    monkeypatch.setattr(api_profile._store, "path", isolated)
    before = migrated.get_profile()
    changed = api_profile.update_profile(
        {"revision": before["revision"], "prompt": "管理员刚修改的管家声音"},
        {"actor": "admin", "persist": True},
    )
    after = migrated.get_profile()
    assert after["prompt"] == "管理员刚修改的管家声音"
    assert after["revision"] == changed["revision"]
