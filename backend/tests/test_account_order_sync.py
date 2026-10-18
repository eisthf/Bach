"""서버가 꺼져 있던 동안의 체결을 계좌 주문체결내역(kt00007)으로 체결 기록에 채우기."""
import calendar
import json

from app.providers.base import Broker
from app.trade_history import TradeHistory

DAY = "20261006"


def _t(hhmmss, day=DAY):
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return calendar.timegm((int(day[:4]), int(day[4:6]), int(day[6:]), h, m, s))


def _order(**kw):
    base = {"code": "008970", "side": "sell", "qty": 722, "price": 1450.0, "order_no": "0088041",
            "time": "09:03:22", "channel": "영웅문S#"}
    return {**base, **kw}


def _fill(order_no, qty, price, hhmmss, identity, side="buy"):
    return {"code": "008970", "side": side, "qty": qty, "price": price, "time": _t(hhmmss),
            "time_source": "execution", "identity": identity, "order_no": order_no}


def test_adds_missing_orders_with_manual_tag(tmp_path):
    th = TradeHistory(tmp_path / "s.trades.json")
    th.record(_fill("0071755", 352, 1414, "09:00:11", "a"))
    th.merge_account_orders(DAY, [
        _order(order_no="0071755", side="buy", qty=352, price=1414.0, time="09:00:10", channel="REST API"),
        _order(),
    ])
    th.flush()
    rows = th.for_code("008970")
    th.close()
    assert len(rows) == 2
    added = rows[1]
    assert added == {"code": "008970", "side": "sell", "qty": 722, "price": 1450.0,
                     "id": "2026-10-06:0088041:account:0", "time": _t("09:03:22"), "time_source": "order",
                     "order_no": "0088041", "source": "account", "channel": "영웅문S#",
                     "strategy": "manual", "reason": "수동(영웅문S#)"}
    # 저장된 파일에도 남는다.
    saved = json.loads((tmp_path / "s.trades.json").read_text(encoding="utf-8"))
    assert [r["order_no"] for r in saved] == ["0071755", "0088041"]


def test_recorded_orders_are_not_duplicated(tmp_path):
    th = TradeHistory(tmp_path / "s.trades.json")
    th.record(_fill("0071755", 200, 1414, "09:00:11", "a"))
    th.record(_fill("0071755", 152, 1414, "09:00:12", "b"))
    orders = [_order(order_no="0071755", side="buy", qty=352, price=1414.0, channel="REST API")]
    th.merge_account_orders(DAY, orders)
    th.merge_account_orders(DAY, orders)
    th.flush()
    rows = th.for_code("008970")
    th.close()
    assert [r["qty"] for r in rows] == [200, 152]
    assert all(r.get("source") != "account" and "strategy" not in r for r in rows)


def test_partial_record_is_topped_up(tmp_path):
    th = TradeHistory(tmp_path / "s.trades.json")
    th.tag_order("0071755", {"strategy": "ulc", "reason": "1차"}, time=_t("09:00:10"))
    th.record(_fill("0071755", 100, 1400, "09:00:11", "a"))
    # 주문 전체 352주, 평균 체결 단가 1414 → 기록되지 않은 252주의 단가 = (1414×352 − 1400×100) / 252
    th.merge_account_orders(DAY, [_order(order_no="0071755", side="buy", qty=352, price=1414.0,
                                         channel="REST API")])
    # 같은 내역을 다시 받아도(다음 시작·장 마감) 이미 채운 수량은 그대로.
    th.merge_account_orders(DAY, [_order(order_no="0071755", side="buy", qty=352, price=1414.0,
                                         channel="REST API")])
    th.flush()
    rows = th.for_code("008970")
    th.close()
    assert [r["qty"] for r in rows] == [100, 252]
    top = rows[1]
    assert top["source"] == "account" and top["time_source"] == "order"
    assert abs(top["price"] - (1414 * 352 - 1400 * 100) / 252) < 1e-9
    assert top["strategy"] == "ulc" and top["reason"] == "1차"   # Bach 주문 태그는 그대로 이어 붙는다


def test_late_fill_notification_replaces_supplement(tmp_path):
    th = TradeHistory(tmp_path / "s.trades.json")
    th.merge_account_orders(DAY, [_order(order_no="0071755", side="buy", qty=352, price=1414.0,
                                         channel="REST API")])
    th.flush()
    # 보충 직후 그 주문의 체결 통지가 늦게 도착: 보충 행이 줄고, 다 오면 사라진다.
    th.record(_fill("0071755", 300, 1414, "09:00:11", "a"))
    th.flush()
    assert [(r["qty"], r.get("source")) for r in th.for_code("008970")] == [(52, "account"), (300, None)]
    th.record(_fill("0071755", 52, 1414, "09:00:12", "b"))
    th.flush()
    rows = th.for_code("008970")
    th.close()
    assert [(r["qty"], r.get("source")) for r in rows] == [(300, None), (52, None)]


def test_hts_order_received_by_bach_is_tagged_manual(tmp_path):
    th = TradeHistory(tmp_path / "s.trades.json")
    th.record(_fill("0088041", 722, 1450, "09:03:23", "a", side="sell"))
    th.merge_account_orders(DAY, [_order()])
    th.flush()
    rows = th.for_code("008970")
    th.close()
    assert len(rows) == 1
    assert rows[0]["strategy"] == "manual" and rows[0]["reason"] == "수동(영웅문S#)"


def test_other_days_are_not_matched(tmp_path):
    th = TradeHistory(tmp_path / "s.trades.json")
    # 주문번호는 거래일마다 새로 매겨진다 — 다른 날 같은 번호는 다른 주문이다.
    th.record({**_fill("0088041", 722, 1450, "09:03:23", "a", side="sell"), "time": _t("09:03:23", "20261005")})
    th.merge_account_orders(DAY, [_order()])
    th.flush()
    rows = th.for_code("008970")
    th.close()
    assert [r.get("source") for r in rows] == [None, "account"]


class _Broker(Broker):
    def __init__(self, history):
        self.history, self.asked = history, []

    def buy(self, code, amount_krw): ...
    def sell(self, code, qty): ...
    def position(self, code): ...
    def all_positions(self): return []

    def order_history(self, day):
        self.asked.append(day)
        result = self.history[day]
        if isinstance(result, Exception):
            raise result
        return result


async def test_hub_sync_fills_gaps_and_failure_leaves_record_unchanged(mock_hub):
    hub, mgr, _ = mock_hub
    hub.trade_history.record(_fill("0071755", 352, 1414, "09:00:11", "a"))
    hub.trade_history.flush()
    before = hub.trade_history.for_code("008970")
    hub.broker = _Broker({"20261005": RuntimeError("HTTP 500"), DAY: [_order()]})
    await hub.sync_account_orders(["20261005", DAY])
    hub.trade_history.flush()
    assert hub.broker.asked == ["20261005", DAY]          # 한 날 실패해도 다음 날은 조회한다
    rows = hub.trade_history.for_code("008970")
    assert rows[:1] == before and rows[1]["order_no"] == "0088041"
    assert not mgr.logs()                                  # 실패는 화면이 아니라 이벤트 로그에만

    hub.broker = _Broker({DAY: RuntimeError("timeout")})
    await hub.sync_account_orders([DAY])
    hub.trade_history.flush()
    assert hub.trade_history.for_code("008970") == rows


def test_default_broker_has_no_history():
    assert _Broker.__mro__[1].order_history(_Broker({}), DAY) == []


def test_sync_days_from_last_record_to_today(mock_hub):
    hub, _, _ = mock_hub
    # 기록이 없으면 오늘 포함 최근 평일 6일(주말 제외).
    assert hub.account_sync_days("20261019") == ["20261012", "20261013", "20261014", "20261015",
                                                 "20261016", "20261019"]
    hub.trade_history.record(_fill("0071755", 1, 1414, "09:00:11", "a"))   # 2026-10-06 기록
    hub.trade_history.flush()
    assert hub.account_sync_days("20261008") == ["20261006", "20261007", "20261008"]
