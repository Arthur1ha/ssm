"""已知人员元数据的本地、原子化持久化。"""

import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from cloud.go2.paths import PEOPLE_FILE
from cloud.go2.execution import require_persist

logger = logging.getLogger(__name__)

_PERSON_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ROLE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


@dataclass(frozen=True)
class KnownPerson:
    person_id: str
    name: str
    role: str
    provider: str
    group_id: str
    face_count: int
    created_ts: float
    updated_ts: float


class PeopleMemory:
    def __init__(self, path: Path | str = PEOPLE_FILE) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    @staticmethod
    def _validate(person: KnownPerson) -> None:
        if not _PERSON_ID_RE.fullmatch(person.person_id):
            raise ValueError("person_id 只能包含字母、数字、下划线和连字符，最长 64 字符")
        if not person.name.strip() or len(person.name.strip()) > 64 or any(ord(c) < 32 for c in person.name):
            raise ValueError("name 必须是 1~64 个可显示字符")
        if not _ROLE_RE.fullmatch(person.role):
            raise ValueError("role 只能包含字母、数字、下划线和连字符，最长 32 字符")
        if not 0 <= person.face_count <= 5:
            raise ValueError("face_count 必须在 0~5 之间")

    def _read_unlocked(self) -> dict[str, KnownPerson]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("version") != 1 or not isinstance(raw.get("people"), dict):
                raise ValueError("unsupported people metadata format")
            result = {}
            for person_id, item in raw["people"].items():
                person = KnownPerson(**item)
                self._validate(person)
                if person.person_id != person_id:
                    raise ValueError("person id/key mismatch")
                result[person_id] = person
            return result
        except Exception:
            logger.exception("[PeopleMemory] 人员元数据损坏，保留原文件: %s", self.path)
            raise

    def _write_unlocked(self, people: dict[str, KnownPerson]) -> None:
        require_persist("modify people memory")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        payload = {"version": 1, "people": {key: asdict(value) for key, value in sorted(people.items())}}
        try:
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.path)
        finally:
            if temp.exists():
                temp.unlink()

    def get(self, person_id: str) -> KnownPerson | None:
        with self._lock:
            return self._read_unlocked().get(person_id)

    def get_owner(self) -> KnownPerson | None:
        with self._lock:
            return next((p for p in self._read_unlocked().values() if p.role == "owner"), None)

    def list(self) -> tuple[KnownPerson, ...]:
        with self._lock:
            return tuple(self._read_unlocked().values())

    def upsert(self, person: KnownPerson) -> KnownPerson:
        require_persist("remember a person")
        self._validate(person)
        with self._lock:
            people = self._read_unlocked()
            owner = next((p for p in people.values() if p.role == "owner" and p.person_id != person.person_id), None)
            if owner:
                raise ValueError("只能登记一个主人")
            now = time.time()
            previous = people.get(person.person_id)
            normalized = replace(
                person,
                name=person.name.strip(),
                created_ts=previous.created_ts if previous else (person.created_ts or now),
                updated_ts=person.updated_ts or now,
            )
            people[person.person_id] = normalized
            self._write_unlocked(people)
            return normalized

    def increment_face_count(self, person_id: str) -> KnownPerson:
        require_persist("add a person's face")
        with self._lock:
            people = self._read_unlocked()
            person = people.get(person_id)
            if person is None:
                raise KeyError(person_id)
            if person.face_count >= 5:
                raise ValueError("每个人最多保存 5 张人脸")
            person = replace(person, face_count=person.face_count + 1, updated_ts=time.time())
            people[person_id] = person
            self._write_unlocked(people)
            return person

    def remove(self, person_id: str) -> bool:
        require_persist("forget a person")
        with self._lock:
            people = self._read_unlocked()
            if person_id not in people:
                return False
            del people[person_id]
            self._write_unlocked(people)
            return True
