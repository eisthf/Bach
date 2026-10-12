"""대금 양봉 '종가 = 고가' 필터의 시간외 위꼬리 확인용 키움 일봉 조회."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models import Bar


def bar(day: str, o, h, low, c) -> Bar:
    t = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
    return Bar(time=t, open=o, high=h, low=low, close=c)


@pytest.fixture
def main(monkeypatch):
    monkeypatch.setenv("ACCOUNTS", "mock")
    monkeypatch.setenv("MOCK_PROVIDER", "mock")
    from app import main as m

    monkeypatch.setattr(m, "now_kst", lambda: datetime(2026, 10, 12, 10))
    monkeypatch.setattr(m, "_SCREEN_DAY_BARS", {})
    return m


def with_kiwoom(main, monkeypatch, get_bars):
    monkeypatch.setattr(main, "manager", SimpleNamespace(
        configs=[SimpleNamespace(id="real", provider="kiwoom", kiwoom_mock=False)],
        hubs={"real": SimpleNamespace(data=SimpleNamespace(get_bars=get_bars))},
    ))


async def test_returns_kiwoom_bar_of_requested_day_and_caches(main, monkeypatch):
    calls = []

    def get_bars(code, interval, extra):
        calls.append(code)
        if code == "999999":
            raise RuntimeError("조회 실패")
        return [bar("2026-10-07", 1, 2, 1, 2), bar("2026-10-08", 227_000, 248_000, 226_000, 243_500),
                bar("2026-10-12", 243_000, 245_000, 240_000, 244_000)]

    with_kiwoom(main, monkeypatch, get_bars)
    r = await main.screener_big_candle_day_bars(date="2026-10-08", codes="000250,999999")
    assert r["available"] and r["failed"] == ["999999"]
    assert r["bars"]["000250"] == {"open": 227_000, "high": 248_000, "low": 226_000, "close": 243_500}
    await main.screener_big_candle_day_bars(date="2026-10-08", codes="000250")
    assert calls.count("000250") == 1                 # 지난 거래일 일봉은 캐시


async def test_unavailable_without_live_kiwoom(main, monkeypatch):
    monkeypatch.setattr(main, "manager", SimpleNamespace(configs=[], hubs={}))
    r = await main.screener_big_candle_day_bars(date="2026-10-08", codes="000250")
    assert r == {"date": "2026-10-08", "available": False, "bars": {}, "failed": []}


async def test_rejects_today_and_bad_input(main):
    with pytest.raises(HTTPException):
        await main.screener_big_candle_day_bars(date="2026-10-12", codes="000250")
    with pytest.raises(HTTPException):
        await main.screener_big_candle_day_bars(date="10/08", codes="000250")
