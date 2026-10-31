"""매매 기록 페이지: 기간별 매매 종목 요약(TradeHistory.summary)과 기본 기간(지난 두 달)."""
import calendar
import json
from datetime import date

from app.main import _months_before
from app.trade_history import TradeHistory


def _t(day, hhmmss="09:30:00"):
    h, m, s = (int(x) for x in hhmmss.split(":"))
    y, mo, d = (int(x) for x in day.split("-"))
    return calendar.timegm((y, mo, d, h, m, s))


def _fill(code, side, qty, price, day, ident, strategy=None):
    row = {"code": code, "side": side, "qty": qty, "price": price, "time": _t(day),
           "identity": ident, "order_no": ident}
    if strategy:
        row["strategy"] = strategy
    return row


def test_summary_groups_by_stock_within_period(tmp_path):
    rows = [
        _fill("111110", "buy", 10, 1_000, "2026-08-01", "a", "zone"),     # 기간 밖
        _fill("111110", "buy", 20, 1_000, "2026-09-10", "b", "zone"),
        _fill("111110", "buy", 10, 900, "2026-09-11", "c", "zone"),
        _fill("111110", "sell", 30, 1_200, "2026-09-20", "d", "manual"),
        _fill("222220", "buy", 5, 3_000, "2026-10-05", "e"),
        _fill("333330", "sell", 1, 100, "2026-11-01", "f"),               # 기간 밖
    ]
    for r in rows:
        r["id"] = r.pop("identity")
    (tmp_path / "s.trades.json").write_text(json.dumps(rows), encoding="utf-8")
    th = TradeHistory(tmp_path / "s.trades.json")
    stocks = th.summary("20260901", "20261031")
    th.close()
    assert [s["code"] for s in stocks] == ["222220", "111110"]                      # 최근 매매 순
    s = stocks[1]
    assert (s["buy_count"], s["buy_qty"], s["buy_amount"]) == (2, 30, 29_000)
    assert (s["sell_count"], s["sell_qty"], s["sell_amount"]) == (1, 30, 36_000)
    assert (s["first_day"], s["last_day"]) == ("2026-09-10", "2026-09-20")
    assert s["strategies"] == ["manual", "zone"]
    assert stocks[0]["strategies"] == [] and stocks[0]["sell_count"] == 0


def test_default_period_is_two_months_back():
    assert _months_before(date(2026, 10, 31), 2) == date(2026, 8, 31)
    assert _months_before(date(2026, 12, 31), 2) == date(2026, 10, 31)
    assert _months_before(date(2026, 4, 30), 2) == date(2026, 2, 28)               # 말일 보정
    assert _months_before(date(2026, 1, 15), 2) == date(2025, 11, 15)              # 해 넘김


async def test_traded_stocks_endpoint_names_and_default_period(mock_hub, monkeypatch):
    from app import main

    hub, _mgr, _clock = mock_hub
    monkeypatch.setattr(main, "_hub", lambda account: hub)
    hub.trade_history.record(_fill("005930", "buy", 1, 70_000, "2026-10-20", "x"))
    hub.trade_history.flush()
    monkeypatch.setattr(hub.data, "stock_name", lambda code: "테스트전자")
    res = await main.traded_stocks("mock", start="2026-09-01", end="2026-10-31")
    assert res["start"] == "2026-09-01" and [s["name"] for s in res["stocks"]] == ["테스트전자"]
    res = await main.traded_stocks("mock", start="2026-10-21", end="2026-10-31")
    assert res["stocks"] == []
