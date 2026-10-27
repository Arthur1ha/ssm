"""ESP32 wall clock for MQTT payloads; ticks remain for elapsed time."""

import time


_EPOCH_YEAR = time.gmtime(0)[0]
if _EPOCH_YEAR == 2000:
    _UNIX_OFFSET = 946684800
elif _EPOCH_YEAR == 1970:
    _UNIX_OFFSET = 0
else:
    raise RuntimeError("unsupported clock epoch")

_MIN_VALID_UNIX = 1704067200  # 2024-01-01; reject an RTC still near its epoch
_RETRY_MS = 60000
_last_attempt_ms = None
_synced = False


def unix_time():
    """Return Unix seconds, or None until the RTC has a plausible date."""
    stamp = int(time.time()) + _UNIX_OFFSET
    return stamp if stamp >= _MIN_VALID_UNIX else None


def sync_if_due():
    """Attempt NTP without blocking the device on failure; retry once a minute."""
    global _last_attempt_ms, _synced
    if _synced:
        return False
    now = time.ticks_ms()
    if (_last_attempt_ms is not None
            and time.ticks_diff(now, _last_attempt_ms) < _RETRY_MS):
        return False
    _last_attempt_ms = now
    try:
        import ntptime
        ntptime.settime()
        _synced = unix_time() is not None
    except Exception as exc:
        print("[Clock] NTP sync failed:", exc)
    if _synced:
        print("[Clock] Unix time synced:", unix_time())
    return _synced
