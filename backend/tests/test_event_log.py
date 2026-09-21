"""브라우저 접속 없이도 전환 원인·기준가 실패를 파일에서 판별할 수 있다."""
import json
from app.event_log import AsyncEventHandler, flush_event_log

import pytest

from app.event_log import configure_event_log, logger, record_event
from app.models import TradeState


@pytest.fixture
def event_file(tmp_path):
    before = set(logger.handlers)
    path = configure_event_log(tmp_path)
    yield path
    for handler in set(logger.handlers) - before:
        logger.removeHandler(handler)
        handler.close()


def events(path):
    flush_event_log()
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_push_and_market_open_are_distinguishable(mock_hub, event_file):
    hub, _, clock = mock_hub
    stock = hub.add_stock("048770")
    hub.push(stock.code)
    hub.push(stock.code)
    clock.open()
    await hub.apply_market_open()
    records = events(event_file)
    pushed = [r for r in records if r.get("reason") == "PUSH"][-1]
    assert pushed["from_state"] == "MONITOR"
    assert pushed["to_state"] == "MANUAL_TRADING"
    assert pushed["account"] == "mock"
    assert pushed["code"] == "048770"
    assert pushed["timestamp"].endswith("+00:00")
    opened = [r for r in records if r.get("reason") == "MARKET_OPEN"][-1]
    assert opened["from_state"] == opened["to_state"] == "MANUAL_TRADING"
    assert not any(r.get("reason") == "AUTO_SETUP_FAILED" for r in records)


async def test_missing_open_price_is_recorded(mock_hub, event_file, monkeypatch):
    hub, _, clock = mock_hub
    stock = hub.add_stock("048770")
    hub.push(stock.code)
    monkeypatch.setattr(hub.data, "prev_close", lambda code: 10000)
    monkeypatch.setattr(hub.data, "day_open", lambda code: None)
    monkeypatch.setattr(hub.data, "get_bars", lambda *args: [])
    clock.open()
    await hub.apply_market_open()
    await stock.setup_task
    records = events(event_file)
    missing = next(r for r in records if r["event"] == "auto_setup_missing_prices")
    assert missing["missing"] == ["Z"]
    assert missing["x"] == 10000
    failure = next(r for r in records if r.get("reason") == "AUTO_SETUP_TIMEOUT")
    assert failure["from_state"] == "AUTO_TRADING"
    assert failure["to_state"] == "MANUAL_TRADING"
    assert stock.machine.state == TradeState.MANUAL_TRADING


async def test_setup_exception_records_stage_without_response_body(mock_hub, event_file,
                                                                  monkeypatch):
    hub, _, clock = mock_hub
    stock = hub.add_stock("048770")
    hub.push(stock.code)

    def fail(code):
        raise RuntimeError("sensitive-response-body")

    monkeypatch.setattr(hub.data, "prev_close", fail)
    clock.open()
    await hub.apply_market_open()
    await stock.setup_task
    error = next(r for r in events(event_file) if r["event"] == "auto_setup_error")
    assert error["stage"] == "prev_close"
    assert error["error_type"] == "RuntimeError"
    assert "sensitive-response-body" not in event_file.read_text(encoding="utf-8")


def test_reopen_appends_and_rotation_retains_events(event_file):
    record_event("before restart")
    handler = next(h for h in logger.handlers if isinstance(h, AsyncEventHandler)
                   and h.baseFilename == str(event_file))
    logger.removeHandler(handler)
    handler.close()
    configure_event_log(event_file.parent)
    configure_event_log(event_file.parent)
    record_event("after restart")
    assert [r["text"] for r in events(event_file)] == ["before restart", "after restart"]
    handler = next(h for h in logger.handlers if isinstance(h, AsyncEventHandler)
                   and h.baseFilename == str(event_file))
    flush_event_log()
    handler.sink.doRollover()
    record_event("after rotation")
    assert events(event_file)[0]["text"] == "after rotation"
    assert len(events(event_file.with_name("events.log.1"))) == 2


async def test_slow_writer_does_not_block_other_tasks_and_overflow_is_counted(tmp_path):
    import asyncio
    import logging
    import threading

    handler = AsyncEventHandler(tmp_path / "slow.log", capacity=1)
    entered, release = threading.Event(), threading.Event()
    original = handler.sink.emit

    def slow(record):
        entered.set()
        release.wait(3)
        original(record)

    handler.sink.emit = slow

    def emit(text):
        record = logging.LogRecord("test", logging.INFO, "", 0, text, (), None)
        record.event = {"text": text}
        handler.handle(record)

    try:
        emit("first")
        assert await asyncio.to_thread(entered.wait, 1)
        # 디스크 쓰기가 막힌 동안 다른 종목 작업을 이벤트 루프에서 실행한다.
        async def other_stock():
            emit("second")
            emit("overflow")
            return not release.is_set()
        assert await asyncio.wait_for(other_stock(), 0.5)
        assert handler.dropped == 1
    finally:
        release.set()
        handler.close()
    rows = [json.loads(line) for line in (tmp_path / "slow.log").read_text(encoding="utf-8").splitlines()]
    assert [r["text"] for r in rows] == ["first", "second"]
    assert rows[-1]["log_dropped_total"] == 1


def test_writer_failure_is_counted_without_stopping_writer(tmp_path):
    import logging
    handler = AsyncEventHandler(tmp_path / "error.log")
    handler.sink.emit = lambda record: (_ for _ in ()).throw(OSError("disk failure"))
    record = logging.LogRecord("test", logging.INFO, "", 0, "test", (), None)
    record.event = {"text": "test"}
    try:
        handler.handle(record)
        handler.queue.join()
        assert handler.write_errors == 1
        assert handler.worker.is_alive()
    finally:
        handler.close()
