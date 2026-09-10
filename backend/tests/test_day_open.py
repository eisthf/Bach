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


def test_previous_date_cache_replaced_by_dated_today_bar(provider, monkeypatch):
    provider._day_open["386380"] = 23200
    provider._day_open_day["386380"] = "20260907"
    monkeypatch.setattr(module.kw, "fetch_day_bars", lambda *a, **k: [
        row("20260907", 23200), row("20260908", 31700),
    ])
    assert provider.day_open("386380") == 31700
    assert provider._day_open_day["386380"] == "20260908"


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
        "10": "31850", "16": "31700", "20": "090020",
    }}]})
    assert provider.day_open("386380") == 31700
    monkeypatch.setattr(module, "now_kst", lambda: datetime(2026, 9, 9, 9, 1, tzinfo=KST))
    monkeypatch.setattr(module.kw, "fetch_day_bars", lambda *a, **k: [])
    assert provider.day_open("386380") is None


def test_zero_tick_open_is_not_replaced_with_current_price(provider, monkeypatch):
    provider._queues["386380"] = asyncio.Queue()
    provider._dispatch_real({"data": [{"type": "0B", "item": "386380", "values": {
        "10": "31850", "16": "0", "20": "090020",
    }}]})
    monkeypatch.setattr(module.kw, "fetch_day_bars", lambda *a, **k: [])
    assert provider.day_open("386380") is None


@pytest.mark.parametrize("minute, expected", [(0, 31700), (3, None)])
def test_only_opening_minute_bar_can_seed_day_open(provider, monkeypatch, minute, expected):
    monkeypatch.setattr(module.kw, "fetch_min_bars", lambda *a, **k: [
        row("20260908", 31700, minute=minute),
    ])
    monkeypatch.setattr(module.kw, "fetch_day_bars", lambda *a, **k: [])
    provider._fetch_bars("386380", 3, 3)
    assert provider.day_open("386380") == expected
