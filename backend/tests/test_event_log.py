"""브라우저 접속 없이도 전환 원인·기준가 실패를 파일에서 판별할 수 있다."""
import json
from logging.handlers import RotatingFileHandler

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
    return [json.loads(line) for line in path.read_text().splitlines()]


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
    records = events(event_file)
    missing = next(r for r in records if r["event"] == "auto_setup_missing_prices")
    assert missing["missing"] == ["Z"]
    assert missing["x"] == 10000
    failure = next(r for r in records if r.get("reason") == "AUTO_SETUP_FAILED")
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
    error = next(r for r in events(event_file) if r["event"] == "auto_setup_error")
    assert error["stage"] == "prev_close"
    assert error["error_type"] == "RuntimeError"
    assert "sensitive-response-body" not in event_file.read_text()


def test_reopen_appends_and_rotation_retains_events(event_file):
    record_event("before restart")
    handler = next(h for h in logger.handlers if isinstance(h, RotatingFileHandler)
                   and h.baseFilename == str(event_file))
    logger.removeHandler(handler)
    handler.close()
    configure_event_log(event_file.parent)
    configure_event_log(event_file.parent)
    record_event("after restart")
    assert [r["text"] for r in events(event_file)] == ["before restart", "after restart"]
    handler = next(h for h in logger.handlers if isinstance(h, RotatingFileHandler)
                   and h.baseFilename == str(event_file))
    handler.doRollover()
    record_event("after rotation")
    assert events(event_file)[0]["text"] == "after rotation"
    assert len(events(event_file.with_name("events.log.1"))) == 2
