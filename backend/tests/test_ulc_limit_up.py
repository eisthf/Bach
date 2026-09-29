"""상한가 도달 청산은 X(전일 마지막 거래가)가 아니라 실제 상한가로 판단한다.

2026-09-28 뷰노: 정규장 종가 6,820 → 시간외 상한가 7,240(X). 9/29 기준가 6,820,
실제 상한가 8,860. X*1.295=9,376 이면 상한가 도달 청산이 영영 발동하지 않는다.
"""
import asyncio
from datetime import datetime

from app.market_clock import KST
from app.models import AutoConfig, Tick, TradeState
from app.providers import kiwoom as module
from app.strategy.ulc import Phase, UlcEngine


def tick(price):
    return Tick(code="338220", price=price, high=price, low=price, open=7_000.0)


def trailing_engine(limit_up):
    sold = []

    async def sell(qty):
        sold.append(qty)
        return 8_860.0

    logs = []
    eng = UlcEngine(code="338220", config=AutoConfig(ulc_trailing=True), x=7_240.0, z=7_000.0,
                    log=logs.append, limit_up=limit_up)
    # 평단 7,800 → 보장 익절선(+15%) 8,970 이라 8,860에서는 상한가 판단만 남는다.
    eng.phase, eng.shares, eng.avg_cost, eng.trail_max = Phase.TRAILING, 10, 7_800.0, 8_800.0
    return eng, sell, sold, logs


async def test_actual_limit_triggers_exit_even_when_x_is_after_hours_price():
    eng, sell, sold, logs = trailing_engine(limit_up=8_860.0)
    await eng.on_tick(tick(8_860.0), None, sell)
    assert sold == [10] and any("상한가 도달" in m for m in logs)


async def test_without_limit_falls_back_to_x_approximation():
    eng, sell, sold, _ = trailing_engine(limit_up=0.0)
    await eng.on_tick(tick(8_860.0), None, sell)
    assert sold == []  # X*1.295=9,376 — 알려진 한계(상한가를 모를 때만)


def test_fetch_price_limits_parses_signed_fields(monkeypatch):
    from app.providers import kiwoom_api as kw

    class Resp:
        status_code = 200

        def json(self):
            return {"return_code": 0, "base_pric": "6820", "upl_pric": "+8860", "lst_pric": "-4780"}

    monkeypatch.setattr(kw, "_post", lambda *a, **k: Resp())
    assert kw.fetch_price_limits("t", "338220") == {"base": 6820, "upper": 8860, "lower": 4780}


def _kiwoom(monkeypatch, limits):
    monkeypatch.setattr(module.kw, "fetch_access_token", lambda *a, **k: module.kw.AccessToken("t", "20990101000000"))
    monkeypatch.setattr(module, "now_kst", lambda: datetime(2026, 9, 29, 9, 0, 20, tzinfo=KST))
    monkeypatch.setattr(module.kw, "fetch_prev_close", lambda *a, **k: 7_240.0)
    calls = []

    def fetch(*a, **k):
        calls.append(1)
        return limits
    monkeypatch.setattr(module.kw, "fetch_price_limits", fetch)
    return module.KiwoomDataProvider("t", "t", mock=True), calls


async def test_setup_passes_actual_limit_to_engine(monkeypatch, mock_hub):
    hub, mgr, clock = mock_hub
    provider, calls = _kiwoom(monkeypatch, {"base": 6_820.0, "upper": 8_860.0, "lower": 4_780.0})
    stock = hub.add_stock("338220")
    stock.task.cancel()
    hub.data = provider
    provider._queues[stock.code] = asyncio.Queue()
    clock.open()
    stock.machine.state = TradeState.AUTO_TRADING
    assert not await hub._start_auto(stock)          # 시가 대기 중 — 상한가는 먼저 받아 둔다
    provider._dispatch_real({"data": [{"type": "0B", "item": "338220", "values": {
        "10": "7000", "16": "7000", "20": "090020", "290": "2", "9081": "KRX", "15": "1"}}]})
    assert await hub._start_auto(stock)
    assert stock.engine.x == 7_240.0 and stock.engine.limit_up == 8_860.0
    assert len(calls) == 1                           # 당일 캐시 — 재시도에 다시 조회하지 않음
    assert any("≠ 기준가 6,820" in text for text in mgr.logs())


async def test_setup_survives_limit_lookup_failure(monkeypatch, mock_hub):
    hub, _, clock = mock_hub
    provider, _ = _kiwoom(monkeypatch, None)
    stock = hub.add_stock("338220")
    stock.task.cancel()
    hub.data = provider
    provider._queues[stock.code] = asyncio.Queue()
    clock.open()
    stock.machine.state = TradeState.AUTO_TRADING
    provider._dispatch_real({"data": [{"type": "0B", "item": "338220", "values": {
        "10": "7000", "16": "7000", "20": "090020", "290": "2", "9081": "KRX", "15": "1"}}]})
    assert await hub._start_auto(stock)
    assert stock.engine.limit_up == 0.0


def test_x_is_previous_day_last_trade_price_including_after_hours(monkeypatch):
    """X 정의 고정: 직전 거래일 일봉 cur_prc(시간외 포함 마지막 가격). 정규장 종가로 되돌리지 말 것."""
    from app.providers import kiwoom_api as kw

    class Resp:
        status_code = 200

        def json(self):
            return {"return_code": 0, "stk_dt_pole_chart_qry": [
                {"dt": "20260929", "cur_prc": "6900"},    # 당일(미확정) — 건너뜀
                {"dt": "20260928", "cur_prc": "+7240"},   # 정규장 6,820 → 시간외 마지막 7,240
                {"dt": "20260923", "cur_prc": "5570"},
            ]}

    monkeypatch.setattr(kw, "_post", lambda *a, **k: Resp())
    assert kw.fetch_prev_close("t", "338220", today="20260929") == 7240
