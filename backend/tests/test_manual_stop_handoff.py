"""분할 완료 후 손절선 도달 시 자동매도/수동 인계 선택 검증."""
from app.models import Position, Tick, TradeState
from app.strategy.ulc import Phase

from conftest import make_engine


def tick(price: float) -> Tick:
    return Tick(code="005930", price=price, high=price, low=price, open=10_000.0)


def holding_engine(*, manual: bool):
    eng = make_engine()
    eng.config.ulc_manual_on_stop = manual
    eng.legs = []  # all_filled=True
    eng.phase = Phase.HOLDING
    eng.shares, eng.avg_cost = 10, 10_000.0
    eng._avg_synced = True
    return eng


async def test_unchecked_stop_still_sells_all():
    sold = []

    async def sell(qty):
        sold.append(qty)
        return 9_400.0

    eng = holding_engine(manual=False)
    await eng.on_tick(tick(9_400.0), sell, sell)
    assert sold == [10]
    assert eng.shares == 0
    assert eng.phase == Phase.DONE
    assert eng.manual_handoff_reason == ""


async def test_checked_stop_skips_sell_and_requests_manual_handoff():
    sold = []

    async def sell(qty):
        sold.append(qty)
        return 9_400.0

    eng = holding_engine(manual=True)
    await eng.on_tick(tick(9_400.0), sell, sell)
    assert sold == []
    assert eng.shares == 10
    assert eng.phase == Phase.DONE
    assert eng.manual_handoff_reason == "손절"


async def test_hub_hands_position_to_manual_without_sell(mock_hub):
    hub, manager, clock = mock_hub
    stock = hub.add_stock("005930", "삼성전자")
    stock.machine.state = TradeState.AUTO_TRADING
    stock.engine = holding_engine(manual=True)
    clock.open()
    hub.broker.position = lambda code: Position(
        code=code, quantity=10, avg_price=10_000.0, current_price=9_400.0)

    await hub._run_engine_tick(stock, tick(9_400.0))

    assert stock.machine.state == TradeState.MANUAL_TRADING
    assert stock.engine is None
    assert "자동 매도하지 않음" in stock.recovery_notice
    assert "계좌 잔량 10주" in stock.recovery_notice
    assert any("STOP_MANUAL_HANDOFF" in text for text in manager.logs())
