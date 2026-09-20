"""재시작 후에도 조회할 수 있는 이벤트 로그. 인증정보·HTTP 본문은 기록하지 않는다."""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
from copy import copy
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


class AsyncEventHandler(logging.Handler):
    """파일 포맷/회전/쓰기 모두 전용 스레드에서 처리. 큐 포화 시 생산자는 대기하지 않는다."""

    def __init__(self, path: Path, capacity: int = 8192):
        super().__init__()
        self.baseFilename = str(path)
        self.sink = RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8")
        self.sink.setFormatter(EventFormatter())
        self.queue = queue.Queue(maxsize=capacity)
        self.dropped = 0
        self.write_errors = 0
        self.stopping = threading.Event()
        # 파일 오류를 stderr로 동기 출력하지 않고 상태로 노출한다.
        self.sink.handleError = self._write_error
        self.worker = threading.Thread(target=self._run, name="bach-event-writer", daemon=True)
        self.worker.start()

    def _write_error(self, record):
        self.write_errors += 1

    def emit(self, record):
        if self.stopping.is_set():
            self.dropped += 1
            return
        try:
            self.queue.put_nowait(copy(record))
        except queue.Full:
            self.dropped += 1

    def _run(self):
        try:
            while not self.stopping.is_set() or not self.queue.empty():
                try:
                    record = self.queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    record.event = dict(record.event, log_dropped_total=self.dropped,
                                        log_write_errors_total=self.write_errors)
                    self.sink.handle(record)
                except Exception:
                    self.write_errors += 1
                finally:
                    self.queue.task_done()
        finally:
            self.sink.close()

    def flush(self):
        # logging.shutdown에서 무한 대기하지 않는다. 테스트의 명시적 대기는 별도 함수.
        pass

    def close(self):
        with self.lock:
            self.stopping.set()
        self.worker.join(timeout=2)
        super().close()


def configure_event_log(directory: Path | None = None) -> Path:
    directory = directory or Path(os.getenv("BACH_LOG_DIR") or
                                 Path(__file__).resolve().parent.parent / "logs")
    directory.mkdir(parents=True, exist_ok=True)
    path = (directory / "events.log").resolve()
    if not any(isinstance(h, AsyncEventHandler) and h.baseFilename == str(path)
               for h in logger.handlers):
        logger.addHandler(AsyncEventHandler(path))
    return path


def flush_event_log() -> None:
    """테스트/명시적 동기화 전용. 매매 경로에서 호출하지 않는다."""
    for handler in logger.handlers:
        if isinstance(handler, AsyncEventHandler):
            handler.queue.join()


def event_log_status() -> list[dict]:
    return [{"queued": h.queue.qsize(), "dropped": h.dropped,
             "write_errors": h.write_errors, "writer_alive": h.worker.is_alive()}
            for h in logger.handlers if isinstance(h, AsyncEventHandler)]


def close_event_log() -> None:
    for handler in list(logger.handlers):
        if isinstance(handler, AsyncEventHandler):
            logger.removeHandler(handler)
            handler.close()


def record_event(text: str, **fields) -> None:
    logger.info(text, extra={"event": {"event": "message", "text": text, **fields}})
