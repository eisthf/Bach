"""[상한가 매매] 1차를 장 시작 단일가에 — 예상체결가로 수량, 상한가 지정가.

09:00 전에 예상체결가를 시가(Z)로 보고 진입·시나리오를 판단해 1차 금액 ÷ 예상가 수량을 상한가 지정가로
낸다. 단일가는 한 가격에 체결되므로 체결가 = 시가다. 시가가 확인되면 못 산 잔량을 취소하고, 체결분을 엔진
1차로 넘긴다(남은 금액은 실제 시가 시나리오의 2차 이후에). PUSH(→대기)하면 그 주문을 취소한다.
"""
import asyncio
from app.market_clock import now_kst

from app.models import OrderResult, TradeState
from app.strategy import ulc
from app.strategy.ulc import Phase, UlcEngine

from conftest import plain_config

X = 10_000.0


def _engine(z: float, **cfg) -> UlcEngine:
    eng = UlcEngine(code="005930", config=plain_config(max_buy_amount=1_000_000, **cfg), x=X, z=z,
                    log=lambda m: None)
    eng.setup()
    return eng


# -- 엔진 ----------------------------------------------------------------------
def test_adopt_full_fill_makes_first_leg_and_resplits_rest():
    eng = _engine(10_600.0)                                  # 실제 시가 +6% → SC2
    eng.adopt_auction("A1", amount=500_000, ordered=47, bought=47, spent=47 * 10_600.0)
    leg0, leg1 = eng.legs
    assert leg0.filled and leg0.bought == 47 and leg0.amount == 500_000
    assert leg1.amount == 500_000 and leg1.target == X      # SC2 2차 ≤ X
    assert eng.shares == 47 and eng.avg_cost == 10_600.0 and eng.order_sent


def test_estimate_scenario_differs_from_actual():
    """예상가로는 SC1(1차 50%)이었는데 실제 시가가 SC3 — 남은 50%를 2·3차에 나눈다."""
    eng = _engine(10_800.0)                                  # +8% → SC3 (3분할)
    eng.adopt_auction("A1", amount=500_000, ordered=48, bought=48, spent=48 * 10_800.0)
    assert [leg.amount for leg in eng.legs] == [500_000, 250_000, 250_000]


def test_partial_fill_proceeds_and_late_fill_counts():
    eng = _engine(10_200.0)
    eng.adopt_auction("A1", amount=500_000, ordered=49, bought=20, spent=20 * 10_200.0)
    leg0 = eng.legs[0]
    assert not leg0.filled and leg0.order_no == "A1" and eng.shares == 20
    eng.on_fill("buy", 29, 10_200.0, "A1")                   # 늦게 온 단일가 체결 통지
    assert leg0.filled and eng.shares == 49
    eng.on_fill("buy", 1, 10_200.0, "A1")                    # 다 산 뒤에 온 것도 1차로 센다
    assert eng.shares == 50


async def test_no_fill_waits_then_falls_back_to_open_buy():
    eng = _engine(10_200.0, ulc_first_buy_ask3=True)
    clock = {"now": 100.0}
    eng.clock = lambda: clock["now"]
    eng.adopt_auction("A1", amount=500_000, ordered=49, bought=0, spent=0.0)
    assert eng.shares == 0 and eng.legs[0].tries == 1
    sent = []

    async def buy(amount):
        sent.append(eng.order_reason)
        eng.last_order_no = "N2"
        return 10_300.0

    async def sell(qty):
        raise AssertionError

    from app.models import Tick
    t = Tick(code="005930", price=10_250.0, high=10_250.0, low=10_250.0, open=10_200.0)
    await eng.on_tick(t, buy, sell)
    assert sent == []                                        # 단일가 체결 통지를 5초 기다린다
    clock["now"] += ulc.IOC_SETTLE_SEC
    await eng.on_tick(t, buy, sell)
    assert sent == ["1차"] and eng.legs[0].tries == 2       # 개장 뒤 매도 3호가 IOC 로


def test_bought_although_actual_open_skips_keeps_position_without_adds():
    """실제 시가가 갭 상한(X×1.15)을 넘어 진입 안 함이어도 이미 산 1차는 들고 손절·익절로 관리."""
    eng = _engine(11_600.0)
    assert eng.phase == Phase.SKIPPED
    eng.adopt_auction("A1", amount=500_000, ordered=47, bought=47, spent=47 * 11_600.0)
    assert eng.phase == Phase.HOLDING and len(eng.legs) == 1 and eng.all_filled
    assert eng.view()["stop_active"]


def test_nothing_bought_and_actual_skip_stays_skipped():
    eng = _engine(11_600.0)
    eng.adopt_auction("A1", amount=500_000, ordered=47, bought=0, spent=0.0)
    assert eng.phase == Phase.SKIPPED and eng.shares == 0


# -- Hub -----------------------------------------------------------------------
def _monitor(hub, code="005930"):
    stock = hub.add_stock(code)
    if stock.task:
        stock.task.cancel()
    stock.machine.state = TradeState.MONITOR
    stock.auction = {"day": now_kst().date().isoformat(), "status": "sending"}
    return stock


def _stub(hub, expected=10_300.0, x=X, upper=13_000.0):
    orders, cancels = [], []
    hub.data.prev_close = lambda code: x
    hub.data.auction_quote = lambda code: {"base": x, "upper": upper, "price": x,
                                           "expected": expected, "expected_qty": 0}

    def buy_limit(code, qty, price, ioc=False):
        orders.append((qty, price, ioc))
        return OrderResult(ok=True, code=code, side="buy", filled_qty=0, price=price, order_no="A1")

    def cancel_order(code, order_no):
        cancels.append(order_no)
        return OrderResult(ok=True, code=code, side="buy", filled_qty=0, price=0, order_no="C1")

    hub.broker.buy_limit = buy_limit
    hub.broker.cancel_order = cancel_order
    hub.data.on_order_fill = hub._on_order_fill              # 체결 통지가 있는 브로커(키움)처럼
    return orders, cancels


async def test_auction_order_uses_expected_qty_and_upper_limit(mock_hub):
    hub, _, clock = mock_hub
    stock = _monitor(hub)
    orders, _ = _stub(hub, expected=10_300.0)                # +3% → SC1, 1차 50만 원
    await hub._auction_order(stock)
    assert orders == [(500_000 // 10_300, 13_000.0, False)]  # 수량 = 금액 ÷ 예상가, 가격 = 상한가(일반 지정가)
    rec = stock.auction
    assert rec["status"] == "sent" and rec["order_no"] == "A1" and rec["scenario"] == 1

    # 09:00 단일가 체결 — 엔진이 생기기 전이라 기록에 모은다
    hub._on_order_fill({"code": "005930", "side": "buy", "qty": 48, "price": 10_350.0, "order_no": "A1"})
    assert rec["bought"] == 48 and rec["spent"] == 48 * 10_350.0
    assert "005930" in hub.stocks                            # 안전망(수동 매매로 옮기기)이 아니다


async def test_auction_skipped_when_expected_gap_too_high(mock_hub):
    hub, mgr, clock = mock_hub
    stock = _monitor(hub)
    orders, _ = _stub(hub, expected=11_600.0)                # +16% ≥ 15%
    await hub._auction_order(stock)
    assert orders == [] and stock.auction["status"] == "skipped"
    assert any("시가 단일가 1차 안 냄" in t for t in mgr.logs())


async def test_push_before_open_cancels_auction_order(mock_hub):
    hub, _, clock = mock_hub
    stock = _monitor(hub)
    _, cancels = _stub(hub)
    await hub._auction_order(stock)
    assert await hub.push("005930") == TradeState.IDLE.value
    assert cancels == ["A1"] and stock.auction["status"] == "cancelled"

    # 취소 전에 체결됐던 게 늦게 오면 — 대기 종목의 관리 밖 체결 → 수동 매매
    hub._on_order_fill({"code": "005930", "side": "buy", "qty": 3, "price": 10_300.0, "order_no": "A1"})
    await asyncio.sleep(0.05)
    assert "005930" in hub.manual_trades.items


async def test_open_cancels_rest_and_hands_fills_to_engine(mock_hub):
    hub, _, clock = mock_hub
    stock = _monitor(hub)
    _, cancels = _stub(hub)
    await hub._auction_order(stock)
    hub._on_order_fill({"code": "005930", "side": "buy", "qty": 30, "price": 10_400.0, "order_no": "A1"})
    stock.machine.state = TradeState.AUTO_TRADING
    clock.open()
    hub.data.day_open = lambda code: 10_400.0
    assert await hub._start_auto(stock)
    assert cancels == ["A1"]                                  # 못 산 18주는 취소
    eng = stock.engine
    assert eng.shares == 30 and eng.avg_cost == 10_400.0 and eng.legs[0].bought == 30
    assert eng.auction_order_no == "A1"


async def test_fully_filled_auction_is_not_cancelled(mock_hub):
    hub, _, clock = mock_hub
    stock = _monitor(hub)
    _, cancels = _stub(hub)
    await hub._auction_order(stock)
    qty = stock.auction["qty"]
    hub._on_order_fill({"code": "005930", "side": "buy", "qty": qty, "price": 10_300.0, "order_no": "A1"})
    stock.machine.state = TradeState.AUTO_TRADING
    clock.open()
    hub.data.day_open = lambda code: 10_300.0
    assert await hub._start_auto(stock)
    assert cancels == [] and stock.engine.legs[0].filled


async def test_demo_market_open_emulates_auction_buy(mock_hub):
    """데모: [장 시작] 순간 시가 단일가 1차를 낸 것으로 흉내 낸다(모의 브로커는 즉시 체결)."""
    hub, _, clock = mock_hub
    stock = hub.add_stock("005930")
    if stock.task:
        stock.task.cancel()
    stock.machine.state = TradeState.MONITOR
    clock.open()
    await hub.apply_market_open()
    rec = stock.auction
    assert rec["status"] == "sent" and rec["bought"] == rec["qty"] > 0
    await asyncio.gather(*(t for t in [stock.setup_task] if t))
    if stock.engine is not None and stock.engine.phase != Phase.SKIPPED:
        assert stock.engine.legs[0].bought == rec["qty"]


async def test_option_off_keeps_open_buy(mock_hub):
    hub, _, clock = mock_hub
    stock = hub.add_stock("005930")
    if stock.task:
        stock.task.cancel()
    stock.machine.state = TradeState.MONITOR
    stock.config.ulc_first_buy_auction = False
    clock.open()
    await hub.apply_market_open()
    assert stock.auction is None
    for t in [stock.setup_task]:
        if t:
            t.cancel()
