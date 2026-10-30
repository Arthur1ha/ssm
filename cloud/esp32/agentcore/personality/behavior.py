"""Lamp personality scores tune reactive evaluation opportunities."""


def light_change_bucket(lux: object, sensitivity: int) -> int | None:
    """Additional change signal within an existing light level; zero ignores noise."""
    if sensitivity < 20 or not isinstance(lux, (int, float)):
        return None
    width = max(10, 100 - sensitivity)
    return int(max(0, lux) // width)
