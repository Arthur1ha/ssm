import json
import threading

import pytest

from cloud.space.people.memory import KnownPerson, PeopleMemory
import cloud.space.people.memory as memory_mod


def person(person_id="go2_owner", role="owner", name="主人", face_count=1):
    return KnownPerson(person_id, name, role, "tencent_iai", "123456", face_count, 1.0, 1.0)


def test_round_trip_owner_and_remove(tmp_path):
    memory = PeopleMemory(tmp_path / "people.json")
    assert memory.list() == ()
    saved = memory.upsert(person())
    assert memory.get("go2_owner") == saved
    assert memory.get_owner() == saved
    assert memory.remove("go2_owner") is True
    assert memory.remove("go2_owner") is False


def test_unique_owner_and_validation(tmp_path):
    memory = PeopleMemory(tmp_path / "people.json")
    memory.upsert(person())
    with pytest.raises(ValueError, match="一个主人"):
        memory.upsert(person("owner2"))
    with pytest.raises(ValueError, match="person_id"):
        memory.upsert(person("bad id", "friend"))


def test_corrupt_file_is_not_overwritten(tmp_path):
    path = tmp_path / "people.json"
    path.write_text("{broken", encoding="utf-8")
    memory = PeopleMemory(path)
    with pytest.raises(json.JSONDecodeError):
        memory.list()
    assert path.read_text(encoding="utf-8") == "{broken"


def test_concurrent_updates_leave_valid_json(tmp_path):
    path = tmp_path / "people.json"
    memory = PeopleMemory(path)
    for index in range(8):
        memory.upsert(person(f"p{index}", "friend", f"朋友{index}"))
    threads = [threading.Thread(target=memory.upsert, args=(person(f"p{i}", "friend", f"更新{i}"),)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload["people"]) == 8
    assert all(item["name"].startswith("更新") for item in payload["people"].values())
    assert "Image" not in path.read_text(encoding="utf-8")


def test_legacy_go2_people_are_read_then_written_to_space(tmp_path, monkeypatch):
    legacy = tmp_path / "go2" / "memory" / "people.json"
    new = tmp_path / "space" / "people.json"
    PeopleMemory(legacy).upsert(person())
    monkeypatch.setattr(memory_mod, "SPACE_PEOPLE_FILE", new)
    monkeypatch.setattr(memory_mod, "LEGACY_PEOPLE_FILE", legacy)
    shared = PeopleMemory(new)
    assert shared.get_owner().person_id == "go2_owner"
    shared.upsert(person("friend1", "friend", "朋友"))
    assert new.exists()
    assert len(shared.list()) == 2
    assert len(PeopleMemory(legacy).list()) == 1
