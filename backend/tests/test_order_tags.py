"""체결 기록의 주문 태그(전략·사유·맥락).

이벤트 로그는 10MB마다 넘겨 쓰다 지워지므로, 어느 전략의 몇 차 매수·어떤 청산이었는지를
체결 기록(trades.json)에 함께 남긴다. 체결 통지(WebSocket)가 주문 응답(REST)보다 먼저 올 수 있어
태그와 체결의 도착 순서를 가리지 않아야 한다.
"""
from datetime import datetime
from types import SimpleNamespace

from app.hub import Hub
from app.market_clock import chart_epoch
from app.models import CandleTradeConfig, OrderResult, Tick
from app.providers.kiwoom import KiwoomDataProvider
from app.providers.mock import MockBroker, MockDataProvider
from app.strategy.big_candle_trade import CandleTradeEngine
from app.strategy.ulc import BuyLeg
from app.trade_history import TradeHistory

from conftest import make_engine, plain_config

ULC_TAG = {"strategy": "ulc", "reason": "1차", "ctx": {"scenario": 1, "x": 10_000.0, "z": 10_200.0}}


def _kiwoom(history, monkeypatch, now=datetime(2026, 9, 21, 9, 5, 0)):
    provider = KiwoomDataProvider.__new__(KiwoomDataProvider)
    provider.on_order_fill = lambda fill: None
    provider.trade_history = history
    monkeypatch.setattr("app.providers.kiwoom.now_kst", lambda: now)
    return provider


def _fill_event(order_no="0071936", fill_no="1037", qty="3"):
    return {"913": "체결", "9001": "A005930", "905": "+매수", "914": "10000", "915": qty,
            "9203": order_no, "908": "090023", "909": fill_no}


def test_fill_before_tag_is_tagged_retroactively_and_persisted(tmp_path, monkeypatch):
    """WS 체결이 REST 주문 응답보다 먼저 와도 태그가 소급해 붙고 파일에 남는다."""
    path = tmp_path / "state.real.trades.json"
    history = TradeHistory(path)
    provider = _kiwoom(history, monkeypatch)
    provider._handle_fill(_fill_event(fill_no="1037"))
    provider._handle_fill(_fill_event(fill_no="1038", qty="2"))   # 같은 주문의 분할 체결
    history.flush()
    assert all("strategy" not in row for row in history.for_code("005930"))
    history.tag_order("0071936", ULC_TAG, time=chart_epoch(datetime(2026, 9, 21, 9, 0, 24)))
    history.close()

    rows = TradeHistory(path).for_code("005930")
    assert len(rows) == 2
    for row in rows:
        assert row["order_no"] == "0071936"
        assert (row["strategy"], row["reason"], row["ctx"]) == ("ulc", "1차", ULC_TAG["ctx"])


def test_tag_before_fill_applies_to_later_fill(tmp_path, monkeypatch):
    history = TradeHistory(tmp_path / "t.json")
    history.tag_order("0071936", ULC_TAG, time=chart_epoch(datetime(2026, 9, 21, 9, 0, 22)))
    _kiwoom(history, monkeypatch)._handle_fill(_fill_event())
    history.close()
    [row] = history.for_code("005930")
    assert row["strategy"] == "ulc" and row["reason"] == "1차"


def test_same_order_no_on_another_day_is_not_tagged(tmp_path, monkeypatch):
    """키움 주문번호는 거래일마다 새로 매겨진다 — 다른 날 같은 번호에 붙으면 안 된다."""
    history = TradeHistory(tmp_path / "t.json")
    _kiwoom(history, monkeypatch, now=datetime(2026, 9, 21, 9, 5))._handle_fill(_fill_event())
    history.tag_order("0071936", ULC_TAG, time=chart_epoch(datetime(2026, 9, 22, 9, 0, 22)))
    history.close()
    [row] = history.for_code("005930")
    assert "strategy" not in row


def test_empty_tag_fields_are_dropped(tmp_path):
    history = TradeHistory(tmp_path / "t.json")
    history.tag_order("A1", {"strategy": "manual", "reason": "", "ctx": None, "other": "x"})
    history.tag_order("", {"strategy": "manual"})   # 주문번호 없음 → 무시
    history.record({"code": "005930", "side": "buy", "qty": 1, "price": 1000, "order_no": "A1"})
    history.close()
    [row] = history.for_code("005930")
    assert row["strategy"] == "manual"
    assert "reason" not in row and "ctx" not in row and "other" not in row


def test_mock_broker_records_order_no_matching_result(tmp_path, monkeypatch):
    data = MockDataProvider()
    data.trade_history = TradeHistory(tmp_path / "mock.trades.json")
    broker = MockBroker(data)
    monkeypatch.setattr(broker, "_price", lambda code: 1000)
    buy = broker.buy("005930", 5000)
    data.trade_history.tag_order(buy.order_no, {"strategy": "manual", "reason": "수동 매수"})
    data.trade_history.close()
    [row] = data.trade_history.for_code("005930")
    assert row["order_no"] == buy.order_no and row["reason"] == "수동 매수"
    # 재시작하면 일련번호가 다시 1부터라 프로세스 시작 시각을 앞에 붙인다.
    assert buy.order_no.startswith(f"MOCK{broker._order_prefix}-")


def test_hub_tag_order_skips_failed_orders_and_isolates_errors():
    tagged = []
    hub = SimpleNamespace(trade_history=SimpleNamespace(tag_order=lambda no, tag: tagged.append((no, tag))))
    ok = OrderResult(ok=True, code="005930", side="buy", filled_qty=1, price=1, order_no="N1")
    Hub.tag_order(hub, ok, "ulc", "1차", {"x": 1})
    Hub.tag_order(hub, OrderResult(ok=False, code="005930", side="buy", filled_qty=0, price=0, order_no="N2"), "ulc")
    Hub.tag_order(hub, OrderResult(ok=True, code="005930", side="buy", filled_qty=1, price=1), "ulc")
    Hub.tag_order(hub, None, "ulc")
    assert tagged == [("N1", {"strategy": "ulc", "reason": "1차", "ctx": {"x": 1}})]

    def broken(no, tag):
        raise RuntimeError("storage")
    hub.trade_history = SimpleNamespace(tag_order=broken)
    Hub.tag_order(hub, ok, "ulc", "1차")   # 예외가 주문 흐름으로 새지 않는다


async def test_ulc_engine_sets_order_reason_before_each_order():
    seen = []

    def engine(**cfg):
        eng = make_engine(account=None)
        eng.config = plain_config(**cfg)
        return eng

    def fns(eng):
        async def buy_fn(amount):
            seen.append(("buy", eng.order_reason))
            return 10_000.0

        async def sell_fn(qty):
            seen.append(("sell", eng.order_reason))
            return 10_600.0
        return buy_fn, sell_fn

    def tick(p):
        return Tick(code="005930", price=p, high=p, low=p, open=10_000.0)

    # 분할 매수 차수
    eng = engine()
    eng.legs = [BuyLeg(target=10_000.0, amount=500_000), BuyLeg(target=9_900.0, amount=500_000)]
    await eng.on_tick(tick(10_000.0), *fns(eng))
    await eng.on_tick(tick(9_900.0), *fns(eng))
    assert [r for side, r in seen if side == "buy"] == ["1차", "2차"]

    # 트레일링 켜진 익절 → 절반 매도
    seen.clear()
    eng = engine(ulc_trailing=True)
    eng.legs = [BuyLeg(target=10_000.0, amount=500_000, filled=True)]
    eng.shares, eng.avg_cost, eng._avg_synced = 50, 10_000.0, True
    await eng.on_tick(tick(10_600.0), *fns(eng))
    assert seen == [("sell", "익절 절반")]

    # 청산 사유는 그대로
    seen.clear()
    await eng._exit_all(fns(eng)[1], "트레일링 스탑 (max 10,600)")
    assert seen == [("sell", "트레일링 스탑 (max 10,600)")]


async def test_candle_engine_sets_order_reason_before_each_order():
    eng = CandleTradeEngine("000001", CandleTradeConfig(total_krw=1_000_000, add_mode="fixed"))
    eng.set_base("2026-09-21", 10_000, 12_000, confirmed=True)
    eng.start()
    eng.begin_day("2026-09-22", 11_000.0, 1)
    seen = []

    async def buy_fn(amount):
        seen.append(("buy", eng.order_reason))
        return price

    async def sell_fn(qty):
        seen.append(("sell", eng.order_reason))
        return qty

    async def tick(p):
        nonlocal price
        price = p
        await eng.on_tick(Tick(code="000001", price=p, high=p, low=p, open=p), "2026-09-22", buy_fn, sell_fn)

    price = 0.0
    await tick(11_000)      # 1차
    await tick(13_200)      # 절반 익절(+20%)
    await tick(14_000)
    await tick(13_300)      # 고점 −5% 트레일링 청산
    assert [s for s, _ in seen] == ["buy", "sell", "sell"]
    assert seen[0][1] == "1차" and seen[1][1] == "절반 익절"
    assert seen[2][1].startswith("트레일링")


def test_candle_manager_tags_strategy_reason_and_context():
    from app.big_candle_trade import CandleItem, CandleTradeManager

    tagged = []
    mgr = CandleTradeManager.__new__(CandleTradeManager)
    mgr.hub = SimpleNamespace(tag_order=lambda res, strategy, reason, ctx: tagged.append((strategy, reason, ctx)))
    eng = CandleTradeEngine("000001", CandleTradeConfig(total_krw=1_000_000))
    eng.set_base("2026-09-21", 10_000, 12_000, confirmed=True)
    eng.order_reason = "1차"
    it = CandleItem(code="000001", name="테스트", engine=eng, created_at="2026-09-21T15:40:00")
    res = OrderResult(ok=True, code="000001", side="buy", filled_qty=45, price=11_000, order_no="N1")
    mgr._tag(it, res)
    [(strategy, reason, ctx)] = tagged
    assert (strategy, reason) == ("candle", "1차")
    assert ctx["base_date"] == "2026-09-21" and ctx["base_open"] == 10_000 and ctx["base_close"] == 12_000
    assert ctx["zone"] == [10_200, 11_800] and ctx["take_profit_pct"] == 20 and ctx["entry_days"] == 5
    assert ctx["created"] == "2026-09-21T15:40:00"


# -- 매매 이벤트(인계) 기록 ----------------------------------------------------

def test_handoff_events_persist_next_to_trades(tmp_path):
    from app.trade_history import events_path_for

    path = tmp_path / "state.real.trades.json"
    assert events_path_for(path).name == "state.real.events.json"
    assert events_path_for(tmp_path / "t.json").name == "t.events.json"
    history = TradeHistory(path)
    history.record_event({"code": "008970", "kind": "handoff", "strategy": "ulc", "reason": "PUSH",
                          "qty": None, "time": 1790948599})
    history.record_event({"code": "", "kind": "handoff"})   # 종목 없음 → 무시
    history.close()
    assert not (tmp_path / "state.real.trades.json").exists()   # 체결 기록 파일은 건드리지 않는다
    restored = TradeHistory(path)
    assert restored.events_for("008970") == [
        {"code": "008970", "kind": "handoff", "strategy": "ulc", "reason": "PUSH", "time": 1790948599}]
    assert restored.events_for("000000") == []
    restored.close()


def test_hub_records_handoff_only_for_auto_to_manual():
    from app.state_machine import TradeState

    events = []
    hub = SimpleNamespace(_log=lambda *a, **k: None, clock=SimpleNamespace(phase=SimpleNamespace(value="OPEN")),
                          trade_history=SimpleNamespace(record_event=events.append))
    manual = SimpleNamespace(code="008970", machine=SimpleNamespace(state=TradeState.IDLE))
    Hub._log_transition(hub, manual, TradeState.AUTO_TRADING, "PUSH")
    Hub._log_transition(hub, manual, TradeState.AUTO_TRADING, "ENGINE_DONE", position_quantity=218)
    Hub._log_transition(hub, manual, TradeState.MONITOR, "PUSH")             # 자동매매 전 → 기록 안 함
    auto = SimpleNamespace(code="008970", machine=SimpleNamespace(state=TradeState.AUTO_TRADING))
    Hub._log_transition(hub, auto, TradeState.MONITOR, "MARKET_OPEN")
    assert [(e["reason"], e["qty"]) for e in events] == [("PUSH", None), ("ENGINE_DONE", 218)]
    assert all(e["kind"] == "handoff" and e["strategy"] == "ulc" for e in events)


def test_candle_handoff_is_recorded_with_campaign():
    from app.big_candle_trade import CandleItem, CandleTradeManager

    events = []
    mgr = CandleTradeManager.__new__(CandleTradeManager)
    mgr.hub = SimpleNamespace(trade_history=SimpleNamespace(record_event=events.append))
    eng = CandleTradeEngine("000250", CandleTradeConfig(total_krw=1_000_000), kind="zone")
    it = CandleItem(code="000250", name="테스트", engine=eng, created_at="2026-10-12T08:55:32")
    eng.on_handoff = mgr._handoff_recorder(it)
    eng.hand_off("사용자 수동 전환")
    assert events == [{"code": "000250", "kind": "handoff", "strategy": "zone", "reason": "사용자 수동 전환",
                       "qty": 0, "ctx": {"created": "2026-10-12T08:55:32"}}]

    def broken(reason):
        raise RuntimeError("storage")
    eng.on_handoff = broken
    eng.hand_off("다시")          # 기록 실패가 인계를 막지 않는다
    assert eng.exit_reason == "다시"


def test_describe_exit_settings():
    from app.models import AutoConfig
    from app.strategy.ulc import describe_exit, exit_settings

    assert describe_exit(exit_settings(AutoConfig())) == "손절 −5% · 익절 +5% · 트레일링 −2%·보장 +15%"
    cfg = AutoConfig(ulc_sl=0.03, ulc_manual_on_stop=True, ulc_trailing=False)
    assert describe_exit(exit_settings(cfg)) == "손절 −3%(자동매도 없이 수동 인계) · 익절 +5% · 트레일링 끔"
