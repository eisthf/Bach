"""게시 경계의 빈 캐시, 부분 게시, 안내 분기."""
from datetime import datetime

import pytest
from fastapi import HTTPException

from app.market_clock import KST
from app.providers import krx_api as krx
from app import screener


@pytest.fixture
def clock(monkeypatch):
    value = [datetime(2026, 9, 10, 7, 59, 50, tzinfo=KST)]
    monkeypatch.setattr(krx, "now_kst", lambda: value[0])
    monkeypatch.setattr(screener, "now_kst", lambda: value[0])
    monkeypatch.setattr(krx._time, "monotonic", lambda: value[0].timestamp())
    krx.clear_cache()
    yield value
    krx.clear_cache()


def test_empty_cache_expires_at_eight(clock, monkeypatch):
    calls = []
    def fetch(day, market):
        calls.append(market)
        return [market] if clock[0].hour >= 8 else []
    monkeypatch.setattr(krx, "_fetch_market", fetch)
    assert krx.daily_quotes("20260909") == []
    clock[0] = datetime(2026, 9, 10, 8, 0, tzinfo=KST)
    assert krx.daily_quotes("20260909") == ["KOSPI", "KOSDAQ"]
    assert len(calls) == 4


def test_partial_market_response_is_not_cached_forever(clock, monkeypatch):
    clock[0] = datetime(2026, 9, 10, 8, 1, tzinfo=KST)
    ready = [False]
    monkeypatch.setattr(krx, "_fetch_market", lambda d, m: [m] if m == "KOSPI" or ready[0] else [])
    assert krx.daily_quotes("20260909") == ["KOSPI"]
    ready[0] = True
    clock[0] = datetime(2026, 9, 10, 8, 1, 31, tzinfo=KST)
    assert krx.daily_quotes("20260909") == ["KOSPI", "KOSDAQ"]


def test_publication_uses_next_weekday():
    assert krx.publication_time("20260911") == datetime(2026, 9, 14, 8, tzinfo=KST)


def test_recent_missing_data_is_pending_and_old_or_weekend_is_not(clock):
    with pytest.raises(screener.DataPending):
        screener.screen_upper_limit("2026-09-09", fetch=lambda d: [], source="krx")
    for date in ("2026-09-06", "2026-09-01"):
        with pytest.raises(screener.NotATradingDay):
            screener.screen_upper_limit(date, fetch=lambda d: [], source="krx")


async def test_pending_has_structured_http_response(clock, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "now_kst", lambda: clock[0])
    def pending(*args):
        raise screener.DataPending(clock[0].date())
    monkeypatch.setattr(main, "screen_upper_limit", pending)
    with pytest.raises(HTTPException) as error:
        await main.screener_upper_limit("2026-09-09", 29, 30)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "DATA_PENDING"
