"""Process-local rotating JSON logs. No two processes share a writable file."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
TRACE_MAX_BYTES = 20 * 1024 * 1024
TRACE_BACKUPS = 4
RETENTION_DAYS = 14


class PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        os.chmod(self.baseFilename, 0o600)
        return stream


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


def _cleanup_old_traces(log_dir: Path) -> None:
    cutoff = time.time() - RETENTION_DAYS * 86400
    for path in log_dir.glob("agent-trace-*.jsonl*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def configure_logging(service: str, *, log_dir: Path | None = None) -> None:
    """Configure one process once; a reload child gets its own trace file."""
    if service not in {"api", "orchestrator", "edge"}:
        raise ValueError("invalid service name")
    os.environ["SSM_LOG_SERVICE"] = service
    log_dir = log_dir or LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_old_traces(log_dir)
    structlog.configure(
        processors=[structlog.processors.JSONRenderer(ensure_ascii=False)],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(getattr(h, "_ssm_process_handler", False) for h in root.handlers):
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(ConsoleFormatter())
        stream._ssm_process_handler = True
        root.addHandler(stream)
    trace = logging.getLogger("agent_trace")
    trace.setLevel(logging.INFO)
    # Full model prompts and tool payloads belong in the trace file. Sending the
    # JSONL record through the process console wraps it in another JSON string.
    trace.propagate = False
    wanted = log_dir / f"agent-trace-{service}-{os.getpid()}.jsonl"
    if not any(getattr(h, "baseFilename", None) == str(wanted) for h in trace.handlers):
        handler = PrivateRotatingFileHandler(wanted, maxBytes=TRACE_MAX_BYTES,
                                             backupCount=TRACE_BACKUPS, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        trace.addHandler(handler)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            handler.setFormatter(ConsoleFormatter())
