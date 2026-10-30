"""Durable, versioned personality profiles shared by the three agents."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from cloud.agent_runtime.json_files import locked_directory, write_record


class RevisionConflict(ValueError):
    """The editor used an older profile snapshot."""


def require_admin_context(context: Any) -> None:
    actor = context.get("actor") if isinstance(context, dict) else getattr(context, "actor", None)
    persist = context.get("persist", True) if isinstance(context, dict) else getattr(context, "persist", None)
    if actor != "admin" or persist is not True:
        raise PermissionError("administrator persistence required")


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"invalid personality profile: {path}")
    return value


class ProfileStore:
    def __init__(self, *, agent_id: str, path: Path, prompt: str,
                 trait_specs: dict[str, dict], migration: Callable[[], dict] | None = None):
        self.agent_id = agent_id
        self.path = path
        self.prompt = prompt
        self.trait_specs = trait_specs
        self.migration = migration

    def _initial(self) -> dict:
        migrated = self.migration() if self.migration else {}
        traits = {key: spec["default"] for key, spec in self.trait_specs.items()}
        traits.update({key: value for key, value in migrated.get("traits", {}).items()
                       if key in traits and type(value) is int and 0 <= value <= 100})
        prompt = migrated.get("prompt")
        return {"schema_version": 1, "agent_id": self.agent_id,
                "prompt": prompt if isinstance(prompt, str) and prompt.strip() else self.prompt,
                "traits": traits, "revision": 1,
                "metadata": migrated.get("metadata", {})}

    def _load(self) -> dict:
        data = _read_json(self.path)
        if data is None:
            data = self._initial()
            write_record(self.path, data)
        if (data.get("schema_version") != 1 or data.get("agent_id") != self.agent_id
                or not isinstance(data.get("traits"), dict)
                or type(data.get("revision")) is not int):
            raise ValueError(f"invalid personality profile: {self.path}")
        return data

    def _view(self, data: dict) -> dict:
        return {"agent_id": self.agent_id, "prompt": data["prompt"],
                "traits": dict(data["traits"]), "trait_specs": deepcopy(self.trait_specs),
                "revision": data["revision"]}

    def get_profile(self) -> dict:
        with locked_directory(self.path.parent):
            return self._view(self._load())

    def update_profile(self, changes: dict, context: Any) -> dict:
        require_admin_context(context)
        if not isinstance(changes, dict) or set(changes) - {"prompt", "traits", "revision"}:
            raise ValueError("unknown personality field")
        if type(changes.get("revision")) is not int:
            raise ValueError("revision is required")
        with locked_directory(self.path.parent):
            data = self._load()
            if changes["revision"] != data["revision"]:
                raise RevisionConflict("personality was changed; reload before saving")
            prompt = changes.get("prompt", data["prompt"])
            if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000:
                raise ValueError("prompt must contain 1–4000 characters")
            incoming = changes.get("traits", {})
            if not isinstance(incoming, dict) or set(incoming) - set(self.trait_specs):
                raise ValueError("unknown personality trait")
            for key, value in incoming.items():
                spec = self.trait_specs[key]
                if type(value) is not int or not spec["min"] <= value <= spec["max"]:
                    raise ValueError(f"{key} must be an integer from {spec['min']} to {spec['max']}")
            updated = {**data, "prompt": prompt.strip(),
                       "traits": {**data["traits"], **incoming}, "revision": data["revision"] + 1}
            write_record(self.path, updated)
            return self._view(updated)

    def evolve(self, delta: dict, date_str: str, *, expected_revision: int | None = None,
               prompt: str | None = None) -> dict:
        """Apply evolution to current values; stale model-generated prompts are discarded."""
        with locked_directory(self.path.parent):
            data = self._load()
            metadata = dict(data.get("metadata") or {})
            if (metadata.get("last_evolved") or "") >= date_str:
                return self._view(data)
            traits = dict(data["traits"])
            for key, change in delta.items():
                if key in traits and type(change) is int:
                    traits[key] = max(0, min(100, traits[key] + change))
            metadata["last_evolved"] = date_str
            if (prompt and expected_revision == data["revision"]
                    and isinstance(prompt, str) and 1 <= len(prompt.strip()) <= 4000):
                data["prompt"] = prompt.strip()
            data.update(traits=traits, metadata=metadata, revision=data["revision"] + 1)
            write_record(self.path, data)
            return self._view(data)

    def metadata(self) -> dict:
        with locked_directory(self.path.parent):
            return dict(self._load().get("metadata") or {})

    def replace_generated_prompt(self, prompt: str, *, expected_revision: int) -> bool:
        """Commit generated expression only if no later edit or evolution won."""
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000:
            return False
        with locked_directory(self.path.parent):
            data = self._load()
            if data["revision"] != expected_revision:
                return False
            data.update(prompt=prompt.strip(), revision=data["revision"] + 1)
            write_record(self.path, data)
            return True
