"""Read the single region-based state machine declaration."""


def primary_region(machine: dict) -> dict:
    return next((region for region in machine.get("regions", [])
                 if region["id"] == machine.get("primary_region")), {})


def transitions(machine: dict):
    for region in machine.get("regions", []):
        yield from region["transitions"]
