"""재시작 후에도 조회할 수 있는 이벤트 로그. 인증정보·HTTP 본문은 기록하지 않는다."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger("bach.events")
logger.setLevel(logging.INFO)
logger.propagate = False
logger.addHandler(logging.NullHandler())


class EventFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "pid": record.process,
            **record.event,
        }, ensure_ascii=False)


def configure_event_log(directory: Path | None = None) -> Path:
    directory = directory or Path(os.getenv("BACH_LOG_DIR") or
                                 Path(__file__).resolve().parent.parent / "logs")
    directory.mkdir(parents=True, exist_ok=True)
    path = (directory / "events.log").resolve()
    if not any(isinstance(h, RotatingFileHandler) and h.baseFilename == str(path)
               for h in logger.handlers):
        handler = RotatingFileHandler(path, maxBytes=10 * 1024 * 1024,
                                      backupCount=10, encoding="utf-8")
        handler.setFormatter(EventFormatter())
        logger.addHandler(handler)
    return path


def record_event(text: str, **fields) -> None:
    logger.info(text, extra={"event": {"event": "message", "text": text, **fields}})
