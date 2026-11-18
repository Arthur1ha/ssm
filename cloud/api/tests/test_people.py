import base64

import cv2
import numpy as np
import pytest

from cloud.api import people as people_api
from cloud.space.people.memory import PeopleMemory
from cloud.space.people.service import SpacePeopleService, _photo_jpeg
from .conftest import login_admin


class FakeProvider:
    group_id = "test-group"

    def __init__(self):
        self.created = []
        self.deleted = []

    async def create_person(self, person_id, name, image):
        self.created.append((person_id, name, image))

    async def remove_from_group(self, person_id):
        self.deleted.append(person_id)


def photo():
    ok, encoded = cv2.imencode(".jpg", np.zeros((200, 200, 3), dtype=np.uint8))
    assert ok
    return base64.b64encode(encoded.tobytes()).decode()


def test_large_original_photo_is_reencoded_for_provider():
    pixels = np.random.default_rng(7).integers(0, 256, (1900, 1900, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", pixels, [cv2.IMWRITE_JPEG_QUALITY, 100])
    assert ok
    original = encoded.tobytes()
    assert len(original) > 4 * 1024 * 1024
    submitted = _photo_jpeg(original)
    assert submitted.startswith(b"\xff\xd8")
    assert len(base64.b64encode(submitted)) <= 5_000_000


@pytest.mark.asyncio
async def test_space_people_api_and_admin_boundary(client, api_env, monkeypatch, tmp_path):
    provider = FakeProvider()
    service = SpacePeopleService(provider, PeopleMemory(tmp_path / "people.json"))
    monkeypatch.setattr(people_api, "space_people", service)
    assert (await client.get("/api/people")).status_code == 401
    csrf = await login_admin(client)
    assert (await client.post("/api/people", json={"name": "主人", "role": "owner", "image": photo()})).status_code == 403
    created = await client.post("/api/people", json={"name": "主人", "role": "owner", "image": photo()}, headers=csrf)
    assert created.status_code == 200
    person_id = created.json()["person_id"]
    assert person_id.startswith("space_person_")
    assert set(created.json()) == {"person_id", "name", "role", "face_count"}
    assert provider.created[0][2].startswith(b"\xff\xd8")
    assert (await client.get("/api/people")).json()[0]["role"] == "owner"
    service_read = await client.get("/api/people", headers={
        "X-SSM-Service-Token": "i" * 32,
        "X-SSM-Session-Id": "other-agent",
        "X-SSM-Actor": "admin",
        "X-SSM-Persist": "false",
    })
    assert service_read.status_code == 200
    assert service_read.json()[0]["person_id"] == person_id
    duplicate = await client.post("/api/people", json={"name": "另一主人", "role": "owner", "image": photo()}, headers=csrf)
    assert duplicate.status_code == 409
    assert (await client.delete(f"/api/people/{person_id}", headers=csrf)).status_code == 200
    assert provider.deleted == [person_id]
    assert (await client.get("/api/people")).json() == []


@pytest.mark.asyncio
async def test_space_people_rejects_invalid_role_and_image(client, api_env, monkeypatch, tmp_path):
    provider = FakeProvider()
    monkeypatch.setattr(people_api, "space_people", SpacePeopleService(provider, PeopleMemory(tmp_path / "people.json")))
    csrf = await login_admin(client)
    for role, image in (("known", photo()), ("friend", base64.b64encode(b"bad-image").decode())):
        response = await client.post("/api/people", json={"name": "测试", "role": role, "image": image}, headers=csrf)
        assert response.status_code == 409
    assert provider.created == []
