"""종가 매매 관리자(Hub 연동): 등록 충돌, 진입, 수동 인계, 재시작 복원·잔고 대조,
체결 라우팅, 예수금 가드, 상한가 매매와의 09:00 주문 우선순위."""
import asyncio
import json

import pytest

from app.models import OrderResult, Position, Tick, TradeState
from app.strategy.close_trade import CtPhase
from conftest import make_engine

CODE = "123450"


class FakeBroker:
    def __init__(self):
        self.qty = 0
        self.price = 10_000.0
        self.buys: list[int] = []
        self.sells: list[int] = []

    def buy(self, code, amount):
        qty = int(amount // self.price)
        self.qty += qty
        self.buys.append(amount)
        return OrderResult(ok=True, code=code, side="buy", filled_qty=qty, price=self.price)

    def sell(self, code, qty):
        self.qty -= qty
        self.sells.append(qty)
        return OrderResult(ok=True, code=code, side="sell", filled_qty=qty, price=self.price)

    def position(self, code):
        return Position(code=code, quantity=self.qty, avg_price=self.price)

    def all_positions(self):
        return [self.position(CODE)] if self.qty else []

    def invalidate(self):
        pass


@pytest.fixture
def hub(mock_hub):
    hub, mgr, clock = mock_hub
    hub.broker = FakeBroker()
    queues: dict[str, asyncio.Queue] = {}

    async def stream_ticks(code):
        q = queues.setdefault(code, asyncio.Queue())
        while True:
            yield await q.get()

    hub.data.stream_ticks = stream_ticks
    hub._ticks = queues
    yield hub
    for it in hub.close_trades.items.values():
        if it.task:
            it.task.cancel()


async def push_tick(hub, price):
    hub.broker.price = price
    await hub._ticks.setdefault(CODE, asyncio.Queue()).put(
        Tick(code=CODE, price=price, high=price, low=price, open=price))
    # 주문은 to_thread로 나가므로 루프 양보만으로는 부족하다.
    for _ in range(10):
        await asyncio.sleep(0.01)


async def test_one_stock_one_strategy(hub):
    hub.close_trades.add(CODE, "테스트")
    with pytest.raises(ValueError):
        hub.add_stock(CODE)
    hub.add_stock("999990")
    with pytest.raises(ValueError):
        hub.close_trades.add("999990")


async def test_enter_closed_market_reserves_then_buys_at_open(hub):
    hub.close_trades.add(CODE, "테스트")
    item = await hub.close_trades.enter(CODE)
    assert item["phase"] == "PENDING_OPEN" and hub.broker.buys == []
    hub.clock.open()
    await push_tick(hub, 10_000)
    eng = hub.close_trades.items[CODE].engine
    assert hub.broker.buys == [200_000] and eng.phase == CtPhase.ACCUMULATING
    await push_tick(hub, 9_500)               # 시가 매수는 곧바로 감시
    assert hub.broker.buys == [200_000, 300_000]


async def test_enter_open_market_buys_now_and_waits_next_day(hub):
    hub.clock.open()
    hub.close_trades.add(CODE, "테스트")
    item = await hub.close_trades.enter(CODE)
    assert item["phase"] == "WAIT_NEXT_DAY" and hub.broker.buys == [200_000]
    await push_tick(hub, 9_000)               # 같은 날: 추가매수 안 함
    assert hub.broker.buys == [200_000]


async def test_cancel_pending_returns_to_draft(hub):
    hub.close_trades.add(CODE, "테스트")
    await hub.close_trades.enter(CODE)
    assert hub.close_trades.cancel(CODE)["phase"] == "DRAFT"


async def test_hand_off_moves_to_trading_list(hub):
    hub.clock.open()
    hub.close_trades.add(CODE, "테스트")
    await hub.close_trades.enter(CODE)
    item = await hub.close_trades.hand_off(CODE)
    assert item["phase"] == "MANUAL"
    stock = hub.get(CODE)
    assert stock is not None and stock.machine.state == TradeState.MANUAL_TRADING
    assert CODE not in hub.close_trades.occupied()
    stock.task.cancel()


async def test_remove_only_when_not_active(hub):
    hub.clock.open()
    hub.close_trades.add(CODE, "테스트")
    await hub.close_trades.enter(CODE)
    with pytest.raises(ValueError):
        await hub.close_trades.remove(CODE)


async def test_fill_event_routes_to_close_trade(hub):
    hub.clock.open()
    hub.close_trades.add(CODE, "테스트")
    await hub.close_trades.enter(CODE)
    hub._on_order_fill({"code": CODE, "side": "buy", "qty": 20, "price": 10_120, "unfilled": 0})
    await asyncio.sleep(0)
    assert hub.close_trades.items[CODE].engine.legs[0].price == pytest.approx(10_120)


def write_saved(hub, phase="WAIT_NEXT_DAY", qty=20):
    legs = [
        {"ratio": 20.0, "amount": 200_000, "status": "filled", "est_qty": qty,
         "est_price": 10_000.0, "fill_qty": 0, "fill_avg": 0.0, "date": "2026-09-21"},
        {"ratio": 30.0, "amount": 300_000, "status": "waiting", "est_qty": 0,
         "est_price": 0.0, "fill_qty": 0, "fill_avg": 0.0, "date": ""},
        {"ratio": 50.0, "amount": 500_000, "status": "waiting", "est_qty": 0,
         "est_price": 0.0, "fill_qty": 0, "fill_avg": 0.0, "date": ""},
    ]
    hub.close_trades._path.write_text(json.dumps({"items": [{
        "code": CODE, "name": "테스트", "config": {},
        "engine": {"phase": phase, "legs": legs, "quiet_date": "2026-09-21",
                   "exit_reason": "", "active_leg": 0},
    }]}), encoding="utf-8")


async def test_restore_resumes_when_account_matches(hub):
    write_saved(hub)
    hub.broker.qty = 20
    await hub.restore()
    it = hub.close_trades.items[CODE]
    assert it.engine.phase == CtPhase.WAIT_NEXT_DAY and it.verified and it.task is not None
    # 종가 매매 종목의 잔고는 [매매] 목록으로 끌려오지 않는다.
    assert hub.get(CODE) is None


async def test_restore_account_empty_ends_trade(hub):
    write_saved(hub)
    hub.broker.qty = 0
    await hub.restore()
    it = hub.close_trades.items[CODE]
    assert it.engine.phase == CtPhase.DONE and "0주" in it.engine.exit_reason


async def test_restore_account_short_hands_off(hub):
    write_saved(hub)
    hub.broker.qty = 5
    await hub.restore()
    await asyncio.sleep(0.05)
    it = hub.close_trades.items[CODE]
    assert it.engine.phase == CtPhase.MANUAL
    assert hub.get(CODE) is not None
    hub.get(CODE).task.cancel()


async def test_restore_without_balance_holds_orders(hub, monkeypatch):
    write_saved(hub)

    def broken():
        raise RuntimeError("잔고 조회 실패")

    monkeypatch.setattr(hub.broker, "all_positions", broken)
    monkeypatch.setattr(hub.broker, "position", lambda code: (_ for _ in ()).throw(RuntimeError()))
    await hub.restore()
    it = hub.close_trades.items[CODE]
    assert not it.verified and "주문 보류" in it.notice
    hub.clock.open()
    await push_tick(hub, 9_000)               # 확인 전엔 주문하지 않는다
    assert hub.broker.buys == []


# ---------------------------------------------------------------------------
# 예수금 가드 / 09:00 주문 우선순위 (상한가 매매와 병행)
# ---------------------------------------------------------------------------
def with_cash(hub, orderable):
    hub.broker.account_summary = lambda: {"orderable": orderable}


async def test_enter_rejected_when_cash_below_first_leg(hub):
    hub.clock.open()
    hub.close_trades.add(CODE, "테스트")
    with_cash(hub, 150_000)                       # 1차 20만원 < 가능 15만원
    with pytest.raises(ValueError, match="주문가능금액"):
        await hub.close_trades.enter(CODE)
    assert hub.broker.buys == []


async def test_enter_warns_when_cash_below_full_plan(hub):
    hub.clock.open()
    hub.close_trades.add(CODE, "테스트")
    with_cash(hub, 400_000)                       # 1차는 되지만 총 100만원엔 부족
    item = await hub.close_trades.enter(CODE)
    assert hub.broker.buys == [200_000]
    assert "부족" in item["notice"] or "실패할 수 있습니다" in item["notice"]


async def test_cash_guard_counts_other_strategies(hub):
    from app.models import TradeState

    hub.clock.open()
    stock = hub.add_stock("999990")               # [매매] 자동매매 예정 종목
    stock.machine.state = TradeState.MONITOR
    stock.config.max_buy_amount = 900_000
    assert hub.close_trades.committed_krw() == 900_000
    hub.close_trades.add(CODE, "테스트")
    with_cash(hub, 1_000_000)                     # 90만 약정 + 1차 20만 > 100만
    with pytest.raises(ValueError, match="다른 전략 약정"):
        await hub.close_trades.enter(CODE)
    stock.task.cancel()


async def test_cash_guard_skipped_without_orderable(hub):
    hub.clock.open()
    hub.close_trades.add(CODE, "테스트")
    hub.broker.account_summary = lambda: None     # mock 등 미지원
    assert (await hub.close_trades.enter(CODE))["phase"] == "WAIT_NEXT_DAY"


async def test_open_buy_yields_until_ulc_first_order(hub):
    from app.models import TradeState

    hub.close_trades.add(CODE, "테스트")
    await hub.close_trades.enter(CODE)            # 장외 → 시가 매수 예약
    stock = hub.add_stock("999990")
    stock.machine.state = TradeState.AUTO_TRADING
    stock.engine = None                           # 셋업(기준가 확인) 중
    hub.clock.open()

    await push_tick(hub, 10_000)
    assert hub.broker.buys == []                  # ULC 1차가 먼저 나갈 때까지 대기

    eng = make_engine()                           # 셋업 후 1차 체결 완료 상태로
    eng.setup()
    eng.legs[0].filled = True
    stock.engine = eng
    await push_tick(hub, 10_000)
    assert hub.broker.buys == [200_000]
    stock.task.cancel()


async def test_open_buy_proceeds_after_yield_timeout(hub, monkeypatch):
    from app.models import TradeState

    monkeypatch.setattr(type(hub.close_trades), "ULC_YIELD_MAX_SEC", 0.0)
    hub.close_trades.add(CODE, "테스트")
    await hub.close_trades.enter(CODE)
    stock = hub.add_stock("999990")
    stock.machine.state = TradeState.AUTO_TRADING
    stock.engine = None
    hub.clock.open()

    await push_tick(hub, 10_000)                  # 첫 틱은 양보(대기 시작)
    assert hub.broker.buys == []
    await push_tick(hub, 10_000)                  # 상한을 넘겨 그대로 진행
    assert hub.broker.buys == [200_000]
    stock.task.cancel()
