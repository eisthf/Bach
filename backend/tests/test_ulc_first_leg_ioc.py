"""[상한가 매매] 1차 매도 3호가 지정가는 IOC, 관리 밖 매수 체결은 수동 매매로.

2026-11-06 엠에스바이오: 1차 매도 3호가 지정가(7,500)가 미체결로 남았는데 엔진은 접수만 보고 1차를
샀다고 여겨 2차 시장가(7,700)를 냈다. 2차 물량으로 익절·트레일링을 마치고 09:11에 끝났는데, 남아 있던
1차 주문이 09:37에 체결돼 66주가 손절·익절 없이 남았다.
- 1차도 IOC — 남는 주문이 없다. 체결로만 1차를 끝내고, 그 전엔 2차를 내지 않는다.
- 한 주도 못 사면 그때 매도 3호가로 재주문(최대 3번), 그래도 못 사면 SKIP.
- 그래도 엔진 없이 매수가 체결되면(HTS 주문 등) 수동 매매로 옮긴다.
"""
import asyncio

from app.models import Tick, TradeState
from app.strategy import ulc
from app.strategy.ulc import BuyLeg, Phase

from conftest import make_engine

X, Z = 8_060.0, 7_470.0


def _tick(price: float) -> Tick:
    return Tick(code="005930", price=price, high=price, low=price, open=Z)


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


def _engine(logs=None):
    """SC1: 1차 시가 매수(매도 3호가 IOC), 2차 ≤ 7,650."""
    eng = make_engine(None, logs, x=X, z=Z)
    eng.config = eng.config.model_copy(update={"ulc_first_buy_ask3": True})
    eng.clock = Clock()
    eng.legs = [BuyLeg(target=Z, amount=500_000), BuyLeg(target=7_650.0, amount=500_000)]
    return eng


class Broker:
    def __init__(self, eng, ask3=7_500.0):
        self.eng = eng
        self.ask3 = ask3
        self.orders = []          # (차수 사유, 금액, 지정가 또는 매도 3호가)

    async def buy(self, amount):
        price = self.eng.order_limit or self.ask3
        self.orders.append((self.eng.order_reason, amount, price))
        self.eng.last_order_no = f"N{len(self.orders)}"
        return price

    async def sell(self, qty):
        return 8_050.0


async def test_unfilled_first_leg_blocks_second_and_is_requoted():
    """엠에스바이오: 1차 IOC 미체결 → 2차 조건가 아래로 와도 2차를 내지 않고, 정리 뒤 1차를 다시 낸다."""
    logs = []
    eng = _engine(logs)
    br = Broker(eng)
    await eng.on_tick(_tick(7_470.0), br.buy, br.sell)
    assert br.orders == [("1차", 500_000, 7_500.0)]
    assert not eng.legs[0].filled and eng.shares == 0 and eng.order_sent

    await eng.on_tick(_tick(7_640.0), br.buy, br.sell)       # 2차 조건(≤ 7,650)이지만 1차 결과 전
    assert len(br.orders) == 1

    eng.clock.now += ulc.IOC_SETTLE_SEC
    br.ask3 = 7_720.0
    await eng.on_tick(_tick(7_700.0), br.buy, br.sell)       # 정리 → 그때 매도 3호가로 재주문
    assert br.orders[1] == ("1차", 500_000, 7_720.0)
    assert any("1차 미체결(1/3)" in m for m in logs)
    eng.on_fill("buy", 64, 7_700.0, "N2")                    # 500,000 // 7,720 = 64주
    assert eng.legs[0].filled and eng.shares == 64 and eng.avg_cost == 7_700.0


async def test_three_misses_skip_the_day():
    logs = []
    eng = _engine(logs)
    br = Broker(eng)
    for _ in range(ulc.FIRST_IOC_TRIES):
        await eng.on_tick(_tick(7_470.0), br.buy, br.sell)
        eng.clock.now += ulc.IOC_SETTLE_SEC
    await eng.on_tick(_tick(7_470.0), br.buy, br.sell)
    assert eng.phase == Phase.SKIPPED and len(br.orders) == ulc.FIRST_IOC_TRIES
    assert any("3번 모두 미체결" in m for m in logs)


async def test_partial_first_leg_proceeds_with_what_was_bought():
    logs = []
    eng = _engine(logs)
    br = Broker(eng)
    await eng.on_tick(_tick(7_470.0), br.buy, br.sell)
    eng.on_fill("buy", 20, 7_490.0, "N1")
    assert not eng.legs[0].filled and eng.shares == 20
    eng.clock.now += ulc.IOC_SETTLE_SEC
    await eng.on_tick(_tick(7_640.0), br.buy, br.sell)       # 정리 → 1차 끝, 같은 틱에 2차
    assert eng.legs[0].filled and any("산 만큼으로 진행" in m for m in logs)
    assert br.orders[1] == ("2차", 500_000, 7_650.0)


async def test_full_first_fill_then_second_leg():
    eng = _engine()
    br = Broker(eng)
    await eng.on_tick(_tick(7_470.0), br.buy, br.sell)
    eng.on_fill("buy", 66, 7_480.0, "N1")
    assert eng.legs[0].filled and eng.shares == 66
    await eng.on_tick(_tick(7_640.0), br.buy, br.sell)
    assert br.orders[1][0] == "2차"


async def test_gap_guard_waits_for_pending_first_order():
    """1차 IOC 결과 대기 중엔 갭 가드로 SKIP 하지 않는다(그 사이 체결되면 보유가 엔진 밖에 남는다)."""
    eng = _engine()
    br = Broker(eng)
    await eng.on_tick(_tick(7_470.0), br.buy, br.sell)
    await eng.on_tick(_tick(X * 1.2), br.buy, br.sell)
    assert eng.phase == Phase.ACCUMULATING
    eng.on_fill("buy", 66, 7_500.0, "N1")
    assert eng.shares == 66


async def test_rejected_first_order_skips():
    eng = _engine()

    async def buy(amount):
        return 0.0

    async def sell(qty):
        raise AssertionError

    await eng.on_tick(_tick(7_470.0), buy, sell)
    assert eng.phase == Phase.SKIPPED


# -- Hub ----------------------------------------------------------------------

def _stock(hub, clock, state=TradeState.AUTO_TRADING):
    stock = hub.add_stock("005930")
    if stock.task:
        stock.task.cancel()
    clock.open()
    stock.machine.state = state
    return stock


async def test_hub_sends_first_leg_as_ask3_ioc(mock_hub):
    hub, _, clock = mock_hub
    stock = _stock(hub, clock)
    eng = _engine()
    eng.position_fn = None
    stock.engine = eng
    calls = []
    real = hub.broker.buy_ask3

    def buy_ask3(code, amount, ioc=False):
        calls.append(ioc)
        return real(code, amount, ioc)
    hub.broker.buy_ask3 = buy_ask3
    hub.broker._price = lambda code: 7_500.0
    await hub._run_engine_tick(stock, _tick(7_470.0))
    assert calls == [True]
    assert eng.legs[0].filled and eng.shares == 66          # mock 은 주문 응답을 곧 체결로 넘긴다


async def test_plan_is_not_rebuilt_after_first_order(mock_hub):
    """1차를 낸 뒤(결과 대기·재주문 사이)엔 시가 보정으로 분할 계획을 다시 세우지 않는다."""
    hub, _, clock = mock_hub
    stock = _stock(hub, clock)
    hub.live = True
    eng = _engine()
    eng.legs[0].tries, eng.legs[0].order_no, eng.legs[0].sent_at = 1, "N1", eng.clock.now
    stock.engine = eng
    hub.broker.buy_ask3 = lambda *a: (_ for _ in ()).throw(AssertionError("재주문"))
    tick = _tick(7_470.0).model_copy(update={"open": 7_400.0, "open_verified": True})
    await hub._run_engine_tick(stock, tick)
    assert eng.z == Z and eng.legs[0].order_no == "N1"


async def test_unmanaged_buy_fill_moves_to_manual(mock_hub):
    """엔진이 끝나 대기인 종목에 늦은 매수 체결이 오면 수동 매매로 옮긴다(엠에스바이오 09:37)."""
    hub, _, clock = mock_hub
    stock = _stock(hub, clock, TradeState.IDLE)
    fill = {"code": "005930", "side": "buy", "qty": 66, "price": 7_500, "unfilled": 0, "order_no": "N1"}
    hub._on_order_fill(fill)
    hub._on_order_fill({**fill, "qty": 1})                    # 분할 체결 — 두 번 옮기지 않는다
    await asyncio.sleep(0.05)
    assert "005930" not in hub.stocks
    item = hub.manual_trades.items["005930"]
    assert "관리하지 않는 매수 체결 66주" in item.notice


async def test_skipped_engine_fill_moves_to_manual(mock_hub):
    hub, _, clock = mock_hub
    stock = _stock(hub, clock)
    eng = _engine()
    eng.phase = Phase.SKIPPED
    stock.engine = eng
    hub._on_order_fill({"code": "005930", "side": "buy", "qty": 5, "price": 7_500, "order_no": "N9"})
    await asyncio.sleep(0.05)
    assert "005930" in hub.manual_trades.items


async def test_monitor_or_active_engine_fills_stay(mock_hub):
    hub, _, clock = mock_hub
    stock = _stock(hub, clock, TradeState.MONITOR)
    hub._on_order_fill({"code": "005930", "side": "buy", "qty": 5, "price": 7_500, "order_no": "N9"})
    stock.machine.state = TradeState.AUTO_TRADING
    stock.engine = _engine()
    stock.engine.legs[0].order_no = "N1"
    hub._on_order_fill({"code": "005930", "side": "buy", "qty": 66, "price": 7_500, "order_no": "N1"})
    await asyncio.sleep(0.05)
    assert "005930" in hub.stocks and "005930" not in hub.manual_trades.items
    assert stock.engine.legs[0].bought == 66
