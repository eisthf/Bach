"""장전 시세/전날 캐시/장중 분봉을 오늘 시가로 오인하는 회귀 방지."""
import asyncio
from datetime import datetime

import pytest

from app.market_clock import KST, chart_epoch
from app.providers import kiwoom as module


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(module.kw, "fetch_access_token", lambda *a, **k: module.kw.AccessToken("test", "20990101000000"))
    monkeypatch.setattr(module, "now_kst", lambda: datetime(2026, 9, 8, 9, 1, tzinfo=KST))
    return module.KiwoomDataProvider("test", "test", mock=True)


def row(day, opened, hour=9, minute=0):
    date = datetime.strptime(day, "%Y%m%d").replace(hour=hour, minute=minute)
    return {"_date": day, "time": chart_epoch(date), "open": opened,
            "high": opened, "low": opened, "close": opened, "volume": 1}


def test_preopen_quote_cannot_seed_strategy_open(provider, monkeypatch):
    monkeypatch.setattr(module, "now_kst", lambda: datetime(2026, 9, 8, 8, 59, tzinfo=KST))
    monkeypatch.setattr(module.kw, "fetch_quote", lambda *a, **k: {
        "name": "스카이랩스", "price": 30550, "open": 23200,
    })
    provider.stock_name("386380")
    assert provider.day_open("386380") is None
    assert "386380" not in provider._day_open


def test_today_rest_bar_cannot_replace_previous_date_cache(provider, monkeypatch):
    provider._day_open["386380"] = 23200
    provider._day_open_day["386380"] = "20260907"
    monkeypatch.setattr(module.kw, "fetch_day_bars", lambda *a, **k: [
        row("20260907", 23200), row("20260908", 31700),
    ])
    assert provider.day_open("386380") is None
    assert provider._day_open_day["386380"] == "20260907"


def test_missing_today_bar_does_not_fall_back_to_stale_open(provider, monkeypatch):
    provider._day_open["386380"] = 23200
    provider._day_open_day["386380"] = "20260907"
    monkeypatch.setattr(module.kw, "fetch_day_bars", lambda *a, **k: [row("20260907", 23200)])
    assert provider.day_open("386380") is None


def test_regular_tick_updates_open_and_next_day_invalidates_it(provider, monkeypatch):
    provider._queues["386380"] = asyncio.Queue()
    provider._day_open["386380"] = 23200
    provider._day_open_day["386380"] = "20260907"
    provider._dispatch_real({"data": [{"type": "0B", "item": "386380", "values": {
        "10": "31850", "16": "31700", "20": "090020", "290": "2", "9081": "KRX", "15": "-1",
    }}]})
    assert provider.day_open("386380") == 31700
    monkeypatch.setattr(module, "now_kst", lambda: datetime(2026, 9, 9, 9, 1, tzinfo=KST))
    monkeypatch.setattr(module.kw, "fetch_day_bars", lambda *a, **k: [])
    assert provider.day_open("386380") is None


def test_zero_tick_open_is_not_replaced_with_current_price(provider, monkeypatch):
    provider._queues["386380"] = asyncio.Queue()
    provider._dispatch_real({"data": [{"type": "0B", "item": "386380", "values": {
        "10": "31850", "16": "0", "20": "090020", "290": "2", "9081": "KRX", "15": "-1",
    }}]})
    monkeypatch.setattr(module.kw, "fetch_day_bars", lambda *a, **k: [])
    assert provider.day_open("386380") is None


@pytest.mark.parametrize("minute, expected", [(0, None), (3, None)])
def test_chart_bars_cannot_seed_day_open(provider, monkeypatch, minute, expected):
    monkeypatch.setattr(module.kw, "fetch_min_bars", lambda *a, **k: [
        row("20260908", 31700, minute=minute),
    ])
    monkeypatch.setattr(module.kw, "fetch_day_bars", lambda *a, **k: [])
    provider._fetch_bars("386380", 3, 3)
    assert provider.day_open("386380") == expected


@pytest.mark.parametrize("volume", [0, None])
def test_empty_today_bar_is_not_a_strategy_open(provider, monkeypatch, volume):
    candidate = row("20260908", 30150)
    candidate["volume"] = volume or 0
    monkeypatch.setattr(module.kw, "fetch_day_bars", lambda *a, **k: [candidate])
    monkeypatch.setattr(module.kw, "fetch_min_bars", lambda *a, **k: [candidate])
    provider._fetch_bars("386380", 3, 3)
    assert provider.day_open("386380") is None


@pytest.mark.parametrize("field,value", [("290", "1"), ("290", ""),
                                         ("9081", "NXT"), ("15", "0"),
                                         ("20", "090200")])
def test_unverified_tick_cannot_seed_open(provider, monkeypatch, field, value):
    provider._queues["386380"] = asyncio.Queue()
    values = {"10": "31850", "16": "31700", "20": "090020",
              "290": "2", "9081": "KRX", "15": "1"}
    values[field] = value
    provider._dispatch_real({"data": [{"type": "0B", "item": "386380", "values": values}]})
    assert not provider.last_tick("386380").open_verified
    assert "386380" not in provider._day_open


def test_chart_response_cannot_overwrite_verified_event(provider, monkeypatch):
    provider._queues["386380"] = asyncio.Queue()
    provider._dispatch_real({"data": [{"type": "0B", "item": "386380", "values": {
        "10": "31850", "16": "31700", "20": "090020",
        "290": "2", "9081": "KRX", "15": "1",
    }}]})
    monkeypatch.setattr(module.kw, "fetch_day_bars", lambda *a, **k: [row("20260908", 23200)])
    provider._fetch_bars("386380", 1440, 1)
    assert provider.day_open("386380") == 31700


def test_waiting_for_event_never_calls_rest(provider, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("시가 대기 중 REST 호출")
    monkeypatch.setattr(provider, "_call", forbidden)
    for _ in range(10):
        assert provider.day_open("386380") is None


@pytest.mark.parametrize("event_first", [True, False])
async def test_setup_handles_both_arrival_orders(provider, monkeypatch, mock_hub, event_first):
    from app.models import TradeState

    hub, _, clock = mock_hub
    stock = hub.add_stock("386380")
    stock.task.cancel()
    hub.data = provider
    provider._queues[stock.code] = asyncio.Queue()
    clock.open()
    stock.machine.state = TradeState.AUTO_TRADING
    calls = []

    def previous(*args, **kwargs):
        calls.append(1)
        return 30150

    monkeypatch.setattr(module.kw, "fetch_prev_close", previous)

    def emit():
        provider._dispatch_real({"data": [{"type": "0B", "item": stock.code, "values": {
            "10": "34600", "16": "34550", "20": "090020",
            "290": "2", "9081": "KRX", "15": "1",
        }}]})

    if event_first:
        emit()
    else:
        assert not await hub._start_auto(stock)
        assert stock.engine is None
    emit()
    assert await hub._start_auto(stock)
    assert stock.engine.z == 34550
    assert stock.engine.scenario == 3
    assert len(calls) == 1  # 성공한 X는 시가 대기 중에도 다시 조회하지 않음
    assert stock.engine.shares == 0
