"""자동매매 다음 거래일 이월.

보유 중에 장이 끝나면 엔진을 끝내지 않고 다음 장에서 그대로 이어간다. 같은 매매의
연속이라 X·Z·분할 계획·평단·트레일링 고점은 진입일 값 그대로이고, 당일 상한가와
계좌 보유 수량만 장 시작 때 새로 맞춘다. 그만두는 것은 사람이 PUSH로 정한다.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

from app.hub import Hub
from app.models import AutoConfig, OrderResult, Position, Tick, TradeState
from app.strategy.ulc import BuyLeg, Phase, UlcEngine


class Broker:
    """계좌 보유를 조작할 수 있는 브로커 스텁. 주문은 기록만 한다."""

    def __init__(self, qty: int = 352, avg: float = 1_413.0) -> None:
        self.qty, self.avg = qty, avg
        self.fail = False
        self.sells: list[int] = []
        self.buy = Mock(side_effect=AssertionError("이월 엔진이 새로 매수하면 안 됨"))

    def all_positions(self):
        if self.fail:
            raise RuntimeError("잔고 조회 실패")
        return [Position(code="008970", quantity=self.qty, avg_price=self.avg)] if self.qty else []

    def position(self, code):
        return Position(code=code, quantity=self.qty, avg_price=self.avg)

    def invalidate(self):
        pass

    def sell(self, code, qty):
        self.sells.append(qty)
        return OrderResult(ok=True, code=code, side="sell", filled_qty=qty, price=1_300.0)


def carried_engine(hub, stock, shares=352) -> UlcEngine:
    """10/2 008970: SC1 1차만 체결된 채 보유 중인 엔진."""
    eng = UlcEngine(code=stock.code, config=stock.config, x=1_423.0, z=1_417.0,
                    log=hub._log, position_fn=hub._position_fn(stock), limit_up=1_885.0)
    eng.phase = Phase.ACCUMULATING
    eng.scenario = 1
    eng.legs = [BuyLeg(target=1_417.0, amount=500_000, filled=True),
                BuyLeg(target=1_351.0, amount=500_000)]
    eng.shares = shares
    eng.on_fill("buy", shares, 1_413.0)
    return eng


def setup(mock_hub, tmp_path, *, shares=352, qty=352):
    hub, mgr, clock = mock_hub
    hub._state_path = tmp_path / "state.real.json"
    hub._persist = Hub._persist.__get__(hub)
    hub.broker = Broker(qty=qty)

    async def limits(stock):
        return {"base": 1_450.0, "upper": 1_670.0, "lower": 1_015.0}
    hub._price_limits = limits
    stock = hub.add_stock("008970", "KBI동양철관")
    stock.config = AutoConfig(max_buy_amount=1_000_000)
    clock.open()
    stock.machine.state = TradeState.AUTO_TRADING
    stock.engine = carried_engine(hub, stock, shares)
    return hub, mgr, clock, stock


def tick(price: float) -> Tick:
    return Tick(code="008970", price=price, high=price, low=price, open=1_400.0,
                open_verified=True, time=1)


def close(hub):
    """장 마감: 시계를 장전으로 되돌리고(manager.market_close와 같은 순서) 허브에 알린다."""
    hub.clock.reset()
    hub.apply_market_close()


async def resume(hub):
    hub.clock.open()
    await hub.apply_market_open()
    await asyncio.gather(*(s.setup_task for s in hub.stocks.values() if s.setup_task))


def saved_entry(hub):
    return json.loads(hub._state_path.read_text(encoding="utf-8"))["stocks"][0]


async def test_close_carries_holding_engine(mock_hub, tmp_path):
    hub, mgr, clock, stock = setup(mock_hub, tmp_path)
    eng = stock.engine
    close(hub)
    assert stock.machine.state == TradeState.AUTO_TRADING
    assert stock.engine is eng
    assert "이월" in stock.recovery_notice
    entry = saved_entry(hub)
    assert entry["state"] == "AUTO_TRADING"
    assert entry["engine"]["x"] == 1_423.0 and entry["engine"]["shares"] == 352


async def test_close_without_holding_ends_as_before(mock_hub, tmp_path):
    hub, mgr, clock, stock = setup(mock_hub, tmp_path)
    stock.engine.shares = 0
    close(hub)
    assert stock.machine.state == TradeState.MANUAL_TRADING
    assert stock.engine is None
    assert "engine" not in saved_entry(hub)


async def test_no_engine_decisions_outside_session(mock_hub, tmp_path):
    """장외(시간외 체결 틱 포함)에는 손절선 아래여도 주문하지 않는다."""
    hub, mgr, clock, stock = setup(mock_hub, tmp_path)
    stock.engine.legs[1].filled = True
    stock.engine.phase = Phase.HOLDING
    close(hub)
    await hub._run_engine_tick(stock, tick(1_200.0))
    assert hub.broker.sells == []


async def test_next_open_resumes_with_entry_day_values(mock_hub, tmp_path):
    hub, mgr, clock, stock = setup(mock_hub, tmp_path)
    eng = stock.engine
    close(hub)
    await resume(hub)
    assert stock.machine.state == TradeState.AUTO_TRADING
    assert stock.engine is eng and not stock.resume_pending
    assert (eng.x, eng.z, eng.avg_cost, eng.shares) == (1_423.0, 1_417.0, 1_413.0, 352)
    assert eng.legs[1].target == 1_351.0 and not eng.legs[1].filled
    assert eng.limit_up == 1_670.0          # 상한가만 당일 기준으로 갱신
    assert stock.recovery_notice == ""


async def test_resumed_engine_trades_on_original_plan(mock_hub, tmp_path):
    """재개 후에는 진입일 계획 그대로: 2차 목표가에서 추가 매수, 새 1차 매수는 없다."""
    hub, mgr, clock, stock = setup(mock_hub, tmp_path)
    close(hub)
    await resume(hub)
    bought = []

    def buy(code, amount):
        bought.append(amount)
        return OrderResult(ok=True, code=code, side="buy", filled_qty=0, price=1_350.0)
    hub.broker.buy = buy
    await hub._run_engine_tick(stock, tick(1_400.0))
    assert bought == []
    await hub._run_engine_tick(stock, tick(1_350.0))
    assert bought == [500_000]               # 2차 ≤ 1,351


async def test_overnight_manual_sell_shrinks_engine(mock_hub, tmp_path):
    """밤사이 직접 판 만큼은 엔진에서도 빼 청산 때 없는 물량을 팔지 않는다."""
    hub, mgr, clock, stock = setup(mock_hub, tmp_path)
    eng = stock.engine
    eng.legs[1].filled = True
    eng.phase = Phase.HOLDING
    close(hub)
    hub.broker.qty = 200
    await resume(hub)
    assert eng.shares == 200 and eng.fill_qty == 200
    await hub._run_engine_tick(stock, tick(1_300.0))   # 손절선(평단×0.95) 아래
    assert hub.broker.sells == [200]


async def test_flat_account_ends_carry(mock_hub, tmp_path):
    hub, mgr, clock, stock = setup(mock_hub, tmp_path)
    close(hub)
    hub.broker.qty = 0
    await resume(hub)
    assert stock.machine.state == TradeState.MANUAL_TRADING
    assert stock.engine is None


async def test_balance_failure_holds_orders_then_hands_off(mock_hub, tmp_path):
    hub, mgr, clock, stock = setup(mock_hub, tmp_path)
    close(hub)
    hub.broker.fail = True
    clock.open()
    await hub.apply_market_open()
    assert stock.resume_pending
    await hub._run_engine_tick(stock, tick(1_000.0))
    assert hub.broker.sells == []            # 대조 전에는 주문 보류
    await asyncio.gather(*(s.setup_task for s in hub.stocks.values() if s.setup_task))
    assert stock.machine.state == TradeState.MANUAL_TRADING
    assert "재개 실패" in stock.recovery_notice


async def test_push_stops_carry(mock_hub, tmp_path):
    hub, mgr, clock, stock = setup(mock_hub, tmp_path)
    close(hub)
    assert hub.push(stock.code) == TradeState.MANUAL_TRADING
    assert stock.engine is None
    assert "engine" not in saved_entry(hub)
    await resume(hub)
    assert stock.engine is None and stock.machine.state == TradeState.MANUAL_TRADING


async def test_restart_before_open_restores_carry(mock_hub, tmp_path):
    hub, mgr, clock, stock = setup(mock_hub, tmp_path)
    close(hub)
    entry = saved_entry(hub)

    hub2 = Hub(hub.cfg, clock, mgr)
    hub2._state_path = hub._state_path
    hub2.broker = Broker()
    hub2._price_limits = hub._price_limits
    try:
        await hub2.restore()
        s2 = hub2.get("008970")
        assert s2.machine.state == TradeState.AUTO_TRADING
        eng = s2.engine
        assert (eng.x, eng.z, eng.avg_cost, eng.shares, eng.phase) == (
            1_423.0, 1_417.0, 1_413.0, 352, Phase.ACCUMULATING)
        assert saved_entry(hub2)["engine"] == entry["engine"]
        await resume(hub2)
        assert s2.engine is eng and not s2.resume_pending
    finally:
        hub2.trade_history.close()
        for s in hub2.stocks.values():
            s.task.cancel()


async def test_restart_during_session_hands_off(mock_hub, tmp_path):
    """장중 재시작은 저장 이후 체결을 알 수 없어 종전대로 수동 인계한다."""
    hub, mgr, clock, stock = setup(mock_hub, tmp_path)
    close(hub)
    clock.open()
    hub2 = Hub(hub.cfg, clock, mgr)
    hub2._state_path = hub._state_path
    hub2.broker = Broker()
    try:
        await hub2.restore()
        s2 = hub2.get("008970")
        assert s2.machine.state == TradeState.MANUAL_TRADING and s2.engine is None
        assert "장중 재시작" in s2.recovery_notice
    finally:
        hub2.trade_history.close()
        for s in hub2.stocks.values():
            s.task.cancel()


def test_engine_round_trip_and_new_session():
    eng = UlcEngine(code="008970", config=AutoConfig(), x=1_423.0, z=1_417.0, log=lambda m: None)
    eng.phase = Phase.TRAILING
    eng.legs = [BuyLeg(target=1_417.0, amount=500, filled=True)]
    eng.shares, eng.avg_cost, eng.trail_max = 100, 1_413.0, 1_500.0
    eng.fill_qty, eng.fill_avg, eng._unconfirmed = 100, 1_413.0, 30
    back = UlcEngine.from_dict("008970", AutoConfig(), eng.to_dict(), log=lambda m: None)
    assert back.to_dict() == eng.to_dict()
    back.begin_session(100, 1_600.0)
    assert back._unconfirmed == 0            # 지정가 미체결은 장 마감으로 소멸
    assert back.limit_up == 1_600.0 and back.trail_max == 1_500.0
