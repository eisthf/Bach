"""대금 양봉 매매 관리자(Hub 연동): 등록 충돌, 감시 시작·중지, 일봉으로 하루 맥락(경과
거래일·전일 종가·기준 캔들 확정), 수동 인계, 재시작 복원·잔고 대조, 체결 라우팅,
예수금 가드, 상한가 매매와의 09:00 주문 우선순위."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import Bar, OrderResult, Position, Tick, TradeState
from app.strategy.big_candle_trade import CzPhase
from conftest import make_engine

CODE = "123450"
BASE = {"date": "2026-09-21", "open": 10_000, "close": 12_000}   # 구간 10,200 ~ 11,800


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


def epoch(d: str) -> int:
    return int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp())


def day_bars(days: dict) -> list:
    """{날짜: (시가, 종가)} → 일봉."""
    return [Bar(time=epoch(d), open=o, high=max(o, c), low=min(o, c), close=c)
            for d, (o, c) in sorted(days.items())]


def weekdays_after(start: str, n: int) -> list[str]:
    out, d = [], date.fromisoformat(start)
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.isoformat())
    return out


@pytest.fixture
def hub(mock_hub, monkeypatch):
    hub, mgr, clock = mock_hub
    hub.broker = FakeBroker()
    queues: dict[str, asyncio.Queue] = {}

    async def stream_ticks(code):
        q = queues.setdefault(code, asyncio.Queue())
        while True:
            yield await q.get()

    hub.data.stream_ticks = stream_ticks
    hub._ticks = queues
    # 기본: 기준일 일봉만 있고 기준일 다음 거래일이 '오늘'
    hub._today = "2026-09-22"
    hub._bars = {BASE["date"]: (BASE["open"], BASE["close"])}
    hub.data.get_bars = lambda code, interval, extra=60: day_bars(hub._bars)
    monkeypatch.setattr(type(hub.candle_trades), "_today", staticmethod(lambda: hub._today))
    yield hub
    for it in hub.candle_trades.items.values():
        if it.task:
            it.task.cancel()


async def push_tick(hub, price):
    hub.broker.price = price
    await hub._ticks.setdefault(CODE, asyncio.Queue()).put(
        Tick(code=CODE, price=price, high=price, low=price, open=price))
    # 주문·일봉 조회는 to_thread로 나가므로 루프 양보만으로는 부족하다.
    for _ in range(10):
        await asyncio.sleep(0.01)


def add(hub, base=BASE):
    return hub.candle_trades.add(CODE, "테스트", dict(base))


def test_add_requires_bullish_base(hub):
    with pytest.raises(ValueError, match="기준 캔들"):
        hub.candle_trades.add(CODE, "테스트", {"date": "2026-09-21", "open": 10_000, "close": 9_000})
    with pytest.raises(ValueError):
        hub.candle_trades.add(CODE, "테스트", {})
    item = add(hub)
    assert item["zone"] == {"low": pytest.approx(10_200), "high": pytest.approx(11_800)}
    assert item["phase"] == "DRAFT"


async def test_one_stock_one_strategy(hub):
    add(hub)
    with pytest.raises(ValueError):
        hub.add_stock(CODE)
    hub.add_stock("999990")
    with pytest.raises(ValueError):
        hub.candle_trades.add("999990", "", dict(BASE))


async def test_start_watches_then_buys_in_zone(hub):
    add(hub)
    item = await hub.candle_trades.start(CODE)
    assert item["phase"] == "WATCHING" and hub.broker.buys == []
    hub.clock.open()
    await push_tick(hub, 12_500)                  # 구간 위
    it = hub.candle_trades.items[CODE]
    assert hub.broker.buys == [] and it.engine.days_elapsed == 1
    await push_tick(hub, 11_000)
    assert hub.broker.buys == [500_000] and it.engine.phase == CzPhase.ACCUMULATING


async def test_no_orders_on_base_day(hub):
    hub._today = BASE["date"]
    add(hub)
    await hub.candle_trades.start(CODE)
    hub.clock.open()
    await push_tick(hub, 11_000)
    assert hub.broker.buys == []


async def test_expiry_counts_trading_days_from_daily_bars(hub):
    days = weekdays_after(BASE["date"], 6)        # D+1 … D+6
    hub._bars.update({d: (11_000, 11_000) for d in days[:5]})
    hub._today = days[5]                          # 6번째 거래일
    add(hub)
    await hub.candle_trades.start(CODE)
    hub.clock.open()
    await push_tick(hub, 11_000)
    it = hub.candle_trades.items[CODE]
    assert it.engine.phase == CzPhase.DONE and "기한 만료" in it.engine.exit_reason
    assert hub.broker.buys == []


async def test_base_candle_confirmed_from_daily_bar(hub):
    hub._bars[BASE["date"]] = (10_000, 13_000)    # 장중 스냅샷보다 높게 마감
    add(hub)
    await hub.candle_trades.start(CODE)
    hub.clock.open()
    await push_tick(hub, 12_500)                  # 확정 구간 10,300 ~ 12,700
    it = hub.candle_trades.items[CODE]
    assert it.engine.base_confirmed and it.engine.zone() == pytest.approx((10_300, 12_700))
    assert hub.broker.buys == [500_000]


async def test_base_day_closed_bearish_ends(hub):
    hub._bars[BASE["date"]] = (10_000, 9_800)
    add(hub)
    await hub.candle_trades.start(CODE)
    hub.clock.open()
    await push_tick(hub, 11_000)
    it = hub.candle_trades.items[CODE]
    assert it.engine.phase == CzPhase.DONE and hub.broker.buys == []


async def test_daily_bar_failure_holds_orders(hub):
    def broken(*_a, **_k):
        raise RuntimeError("일봉 조회 실패")

    hub.data.get_bars = broken
    add(hub)
    await hub.candle_trades.start(CODE)
    hub.clock.open()
    await push_tick(hub, 11_000)
    assert hub.broker.buys == []


async def test_zone_break_stop_via_prev_close(hub):
    add(hub)
    await hub.candle_trades.start(CODE)
    hub.clock.open()
    await push_tick(hub, 11_000)                  # D+1 1차 매수
    hub._bars["2026-09-22"] = (11_000, 10_000)    # 하단(10,200) 아래 마감
    hub._today = "2026-09-23"
    await push_tick(hub, 10_100)
    it = hub.candle_trades.items[CODE]
    assert it.engine.phase == CzPhase.DONE and hub.broker.sells == [45]
    assert hub.broker.qty == 0


async def test_stop_returns_to_draft(hub):
    add(hub)
    await hub.candle_trades.start(CODE)
    item = await hub.candle_trades.stop(CODE)
    assert item["phase"] == "DRAFT"
    assert hub.candle_trades.items[CODE].task is None


async def test_hand_off_moves_to_trading_list(hub):
    add(hub)
    await hub.candle_trades.start(CODE)
    with pytest.raises(ValueError):               # 보유 전엔 수동 전환이 아니라 중지
        await hub.candle_trades.hand_off(CODE)
    hub.clock.open()
    await push_tick(hub, 11_000)
    item = await hub.candle_trades.hand_off(CODE)
    assert item["phase"] == "MANUAL"
    stock = hub.get(CODE)
    assert stock is not None and stock.machine.state == TradeState.MANUAL_TRADING
    assert CODE not in hub.candle_trades.occupied()
    stock.task.cancel()


async def test_remove_only_when_not_active(hub):
    add(hub)
    await hub.candle_trades.start(CODE)
    with pytest.raises(ValueError):
        await hub.candle_trades.remove(CODE)


async def test_fill_event_routes_to_candle_trade(hub):
    add(hub)
    await hub.candle_trades.start(CODE)
    hub.clock.open()
    await push_tick(hub, 11_000)
    hub._on_order_fill({"code": CODE, "side": "buy", "qty": 45, "price": 11_020, "unfilled": 0})
    await asyncio.sleep(0)
    assert hub.candle_trades.items[CODE].engine.legs[0].price == pytest.approx(11_020)


def write_saved(hub, phase="ACCUMULATING", qty=45):
    legs = [
        {"ratio": 50.0, "amount": 500_000, "status": "filled", "est_qty": qty,
         "est_price": 11_000.0, "fill_qty": 0, "fill_avg": 0.0, "date": "2026-09-22"},
        {"ratio": 50.0, "amount": 500_000, "status": "waiting", "est_qty": 0,
         "est_price": 0.0, "fill_qty": 0, "fill_avg": 0.0, "date": ""},
    ]
    hub.candle_trades._path.write_text(json.dumps({"items": [{
        "code": CODE, "name": "테스트", "config": {},
        "engine": {"phase": phase, "legs": legs, "active_leg": 0,
                   "base": {**BASE, "confirmed": True}},
    }]}), encoding="utf-8")


async def test_restore_resumes_when_account_matches(hub):
    write_saved(hub)
    hub.broker.qty = 45
    await hub.restore()
    it = hub.candle_trades.items[CODE]
    assert it.engine.phase == CzPhase.ACCUMULATING and it.verified and it.task is not None
    # 대금 양봉 매매 종목의 잔고는 [매매] 목록으로 끌려오지 않는다.
    assert hub.get(CODE) is None


async def test_restore_account_empty_ends_trade(hub):
    write_saved(hub)
    hub.broker.qty = 0
    await hub.restore()
    it = hub.candle_trades.items[CODE]
    assert it.engine.phase == CzPhase.DONE and "0주" in it.engine.exit_reason


async def test_restore_account_short_hands_off(hub):
    write_saved(hub)
    hub.broker.qty = 5
    await hub.restore()
    await asyncio.sleep(0.05)
    it = hub.candle_trades.items[CODE]
    assert it.engine.phase == CzPhase.MANUAL
    assert hub.get(CODE) is not None
    hub.get(CODE).task.cancel()


async def test_restore_without_balance_holds_orders(hub, monkeypatch):
    write_saved(hub)

    def broken():
        raise RuntimeError("잔고 조회 실패")

    monkeypatch.setattr(hub.broker, "all_positions", broken)
    monkeypatch.setattr(hub.broker, "position", lambda code: (_ for _ in ()).throw(RuntimeError()))
    await hub.restore()
    it = hub.candle_trades.items[CODE]
    assert not it.verified and "주문 보류" in it.notice
    hub._today = "2026-09-23"
    hub.clock.open()
    await push_tick(hub, 10_400)                  # 2차 조건이지만 확인 전엔 주문하지 않는다
    assert hub.broker.buys == []


async def test_legacy_close_trade_file_is_reported(hub):
    hub._state_path.with_suffix(".close.json").write_text("{}", encoding="utf-8")
    await hub.restore()
    assert any("종가 매매 기록" in t for t in hub.manager.logs())


# ---------------------------------------------------------------------------
# 예수금 가드 / 09:00 주문 우선순위 (상한가 매매와 병행)
# ---------------------------------------------------------------------------
def with_cash(hub, orderable):
    hub.broker.account_summary = lambda: {"orderable": orderable}


async def test_start_rejected_when_cash_below_first_leg(hub):
    add(hub)
    with_cash(hub, 400_000)                       # 1차 50만원 > 가능 40만원
    with pytest.raises(ValueError, match="주문가능금액"):
        await hub.candle_trades.start(CODE)
    assert hub.candle_trades.items[CODE].engine.phase == CzPhase.DRAFT


async def test_start_warns_when_cash_below_full_plan(hub):
    add(hub)
    with_cash(hub, 600_000)                       # 1차는 되지만 총 100만원엔 부족
    item = await hub.candle_trades.start(CODE)
    assert "실패할 수 있습니다" in item["notice"]


async def test_cash_guard_counts_other_strategies(hub):
    stock = hub.add_stock("999990")               # [매매] 자동매매 예정 종목
    stock.machine.state = TradeState.MONITOR
    stock.config.max_buy_amount = 900_000
    assert hub.candle_trades.committed_krw() == 900_000
    add(hub)
    with_cash(hub, 1_000_000)                     # 90만 약정 + 1차 50만 > 100만
    with pytest.raises(ValueError, match="다른 전략 약정"):
        await hub.candle_trades.start(CODE)
    stock.task.cancel()


async def test_cash_guard_skipped_without_orderable(hub):
    add(hub)
    hub.broker.account_summary = lambda: None     # mock 등 미지원
    assert (await hub.candle_trades.start(CODE))["phase"] == "WATCHING"


async def test_first_buy_yields_until_ulc_first_order(hub):
    add(hub)
    await hub.candle_trades.start(CODE)
    stock = hub.add_stock("999990")
    stock.machine.state = TradeState.AUTO_TRADING
    stock.engine = None                           # 셋업(기준가 확인) 중
    hub.clock.open()

    await push_tick(hub, 11_000)
    assert hub.broker.buys == []                  # ULC 1차가 먼저 나갈 때까지 대기

    eng = make_engine()                           # 셋업 후 1차 체결 완료 상태로
    eng.setup()
    eng.legs[0].filled = True
    stock.engine = eng
    await push_tick(hub, 11_000)
    assert hub.broker.buys == [500_000]
    stock.task.cancel()


async def test_first_buy_proceeds_after_yield_timeout(hub, monkeypatch):
    monkeypatch.setattr(type(hub.candle_trades), "ULC_YIELD_MAX_SEC", 0.0)
    add(hub)
    await hub.candle_trades.start(CODE)
    stock = hub.add_stock("999990")
    stock.machine.state = TradeState.AUTO_TRADING
    stock.engine = None
    hub.clock.open()

    await push_tick(hub, 11_000)                  # 첫 틱은 양보(대기 시작)
    assert hub.broker.buys == []
    await push_tick(hub, 11_000)                  # 상한을 넘겨 그대로 진행
    assert hub.broker.buys == [500_000]
    stock.task.cancel()


# ---------------------------------------------------------------------------
# 구간 매매(kind="zone")
# ---------------------------------------------------------------------------
async def test_zone_trade_counts_days_from_watch_start(hub):
    item = hub.candle_trades.add_zone(CODE, "테스트")
    assert item["kind"] == "zone" and item["zone"] is None and item["zone_editable"]
    with pytest.raises(ValueError):
        await hub.candle_trades.start(CODE)   # 구간 미지정
    hub.candle_trades.set_zone(CODE, 10_200, 11_800)
    await hub.candle_trades.start(CODE)
    hub._bars = {d: (11_000, 11_000) for d in ["2026-09-18", "2026-09-21"]}
    hub.clock.open()
    await push_tick(hub, 12_000)
    it = hub.candle_trades.items[CODE]
    assert it.engine.watch_from == "2026-09-22" and it.engine.days_elapsed == 1
    await push_tick(hub, 11_000)
    assert hub.broker.buys == [500_000]
    assert hub.candle_trades.payload(CODE)["zone_editable"] is False
    with pytest.raises(ValueError):
        hub.candle_trades.set_zone(CODE, 10_000, 11_000)
    assert any("[123450] 구간매매:" in t for t in hub.manager.logs())


async def test_zone_trade_expires_after_entry_days(hub):
    hub.candle_trades.add_zone(CODE, "테스트", {"low": 10_200, "high": 11_800})
    await hub.candle_trades.start(CODE)
    it = hub.candle_trades.items[CODE]
    it.engine.watch_from = "2026-09-15"
    days = weekdays_after("2026-09-14", 5)    # 9/15 … 9/21: 5거래일 지남
    hub._bars = {d: (11_000, 11_000) for d in days}
    hub.clock.open()
    await push_tick(hub, 11_000)
    assert it.engine.phase == CzPhase.DONE and hub.broker.buys == []


async def test_zone_and_candle_share_one_stock_rule(hub):
    hub.candle_trades.add_zone(CODE, "테스트")
    with pytest.raises(ValueError, match="구간 매매"):
        add(hub)
    with pytest.raises(ValueError):
        hub.add_stock(CODE)
