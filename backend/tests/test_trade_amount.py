"""일봉 거래대금(amount) — 150억 스트라이프 표시의 데이터 원천 검증."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

from app import main
from app.market_clock import KST
from app.models import Bar, Tick
from app.providers import kiwoom as module
from app.providers.mock import MockDataProvider


def test_realtime_tick_parses_cumulative_amount_in_won(monkeypatch):
    monkeypatch.setattr(module.kw, "fetch_access_token",
                        lambda *a, **k: module.kw.AccessToken("test", "20990101000000"))
    monkeypatch.setattr(module, "now_kst", lambda: datetime(2026, 9, 8, 9, 1, tzinfo=KST))
    provider = module.KiwoomDataProvider("test", "test", mock=True)
    provider._queues["001520"] = asyncio.Queue()
    provider._dispatch_real({"data": [{"type": "0B", "item": "001520", "values": {
        "10": "+5040", "15": "+10", "14": "9395",  # FID 14 = 누적거래대금(백만원)
    }}]})
    assert provider.last_tick("001520").amount == 9_395_000_000


def test_today_daily_bar_amount_follows_tick_cumulative(monkeypatch):
    day = 20_000 * 1440 * 60  # 임의의 자정 epoch
    stale = Bar(time=day, open=100, high=110, low=90, close=100, volume=10, amount=1e9)
    tick = Tick(code="001520", price=105, high=110, low=90, open=100,
                amount=16e9, time=day + 36_000)
    monkeypatch.setattr(main, "_hub", lambda account: SimpleNamespace(data=SimpleNamespace(
        get_bars=lambda *a: [stale], last_tick=lambda code: tick)))
    result = main.get_bars("real", "001520", 1440, 60, False)
    assert result["bars"][-1]["amount"] == 16e9
    assert result["bars"][-1]["close"] == 105

    # 틱이 거래대금을 모르면(0) 일봉 값을 유지한다.
    tick.amount = 0
    result = main.get_bars("real", "001520", 1440, 60, False)
    assert result["bars"][-1]["amount"] == 1e9


def test_mock_daily_bars_carry_amount():
    bars = MockDataProvider().get_bars("005930", 1440)
    assert all(b.amount == b.volume * b.close for b in bars[:-1])
