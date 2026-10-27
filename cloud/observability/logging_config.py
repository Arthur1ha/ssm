"""Process-local rotating JSON logs. No two processes share a writable file."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import structlog

LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
RETENTION_DAYS = 14
_event_log_dir = LOG_DIR


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        output = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname.lower(), "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            output["exception"] = self.formatException(record.exc_info)
        return json.dumps(output, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")


class _DowngradeUnitreeDataChannelLogs(logging.Filter):
    """Move the Go2 library's raw telemetry from INFO to opt-in DEBUG."""

    def filter(self, record: logging.LogRecord) -> bool:
        raw_telemetry = (
            record.name == "root"
            and record.levelno == logging.INFO
            and record.msg == "Received message on data channel: %s"
        )
        if raw_telemetry:
            logging.getLogger("unitree_webrtc_connect.telemetry").debug(
                record.msg, *record.args,
            )
        return not raw_telemetry


def _cleanup_old_sessions(log_dir: Path) -> None:
    cutoff = time.time() - RETENTION_DAYS * 86400
    for path in log_dir.glob("session-*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def write_agent_event(record: dict) -> None:
    """Append one complete event to its session; open only while writing."""
    session_id = record.get("session_id") or "system"
    if not isinstance(session_id, str):
        session_id = str(session_id)
    safe_id = (session_id if re.fullmatch(r"[A-Za-z0-9_-]{1,160}", session_id)
               else hashlib.sha256(session_id.encode()).hexdigest())
    path = _event_log_dir / f"session-{safe_id}.jsonl"
    line = (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        start = os.lseek(fd, 0, os.SEEK_END)
        view = memoryview(line)
        try:
            while view:
                written = os.write(fd, view)
                if written == 0:
                    raise OSError("session log write made no progress")
                view = view[written:]
        except OSError:
            os.ftruncate(fd, start)
            raise
    finally:
        os.close(fd)


def configure_logging(service: str, *, log_dir: Path | None = None) -> None:
    """Configure console logging and the destination for session events."""
    if service not in {"api", "orchestrator", "edge"}:
        raise ValueError("invalid service name")
    os.environ["SSM_LOG_SERVICE"] = service
    global _event_log_dir
    log_dir = log_dir or LOG_DIR
    _event_log_dir = log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_old_sessions(log_dir)
    structlog.configure(
        processors=[structlog.processors.JSONRenderer(ensure_ascii=False)],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for existing in tuple(root.filters):
        if isinstance(existing, _DowngradeUnitreeDataChannelLogs):
            root.removeFilter(existing)
    if service == "api":
        root.addFilter(_DowngradeUnitreeDataChannelLogs())
    if not any(getattr(h, "_ssm_process_handler", False) for h in root.handlers):
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(ConsoleFormatter())
        stream._ssm_process_handler = True
        root.addHandler(stream)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            handler.setFormatter(ConsoleFormatter())
