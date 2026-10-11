"""스크리너 차트가 완료된 정규장 하루를 온전히 반환하는지 확인한다."""
from datetime import datetime, timezone
from types import SimpleNamespace
import time

from app import main
from app.models import Bar
from app.providers import kiwoom_api as kw


def bar(day, minute):
    dt = datetime.strptime(f"{day} {minute // 60:02d}:{minute % 60:02d}", "%Y-%m-%d %H:%M")
    return Bar(time=int(dt.replace(tzinfo=timezone.utc).timestamp()), open=100,
               high=110, low=90, close=100, volume=10)


def test_screener_chart_uses_last_completed_regular_session(monkeypatch):
    previous = [bar("2026-09-17", 540 + i * 3) for i in range(130)]
    today = [bar("2026-09-18", 540 + i * 3) for i in range(10)]
    after_hours = [bar("2026-09-17", 19 * 60 + 42)]
    calls = []

    def get_bars(code, interval, lookback):
        calls.append((code, interval, lookback))
        return previous + after_hours + today

    monkeypatch.setattr(main, "_hub", lambda account: SimpleNamespace(
        data=SimpleNamespace(get_bars=get_bars)))
    monkeypatch.setattr(main, "now_kst", lambda: datetime(2026, 9, 18, 10, 0))
    result = main.get_bars("real", "001520", 3, 60, True)
    assert calls == [("001520", 3, 200)]
    assert result["session_date"] == "2026-09-17"
    assert result["day_start_index"] == 0
    assert len(result["bars"]) == 130
    assert result["bars"][0]["time"] == previous[0].time
    assert result["bars"][-1]["time"] == previous[-1].time

    monkeypatch.setattr(main, "now_kst", lambda: datetime(2026, 9, 18, 16, 0))
    result = main.get_bars("real", "001520", 3, 60, True)
    assert result["session_date"] == "2026-09-18"
    assert result["day_start_index"] == 60
    assert len(result["bars"]) == 70


def test_kiwoom_uses_latest_response_date_on_weekend(monkeypatch):
    class Response:
        status_code = 200
        def __init__(self, page, continued):
            self.page = page
            self.headers = {"cont-yn": "Y" if continued else "N", "next-key": "next" if continued else ""}
        def json(self):
            return {"return_code": 0, "stk_min_pole_chart_qry": self.page}

    def raw(day, minute):
        return {"cntr_dt": f"{day}{minute // 60:02d}{minute % 60:02d}00",
                "open_pric": "100", "high_pric": "110", "low_pric": "90",
                "cur_prc": "100", "trde_qty": "10"}

    latest = [raw("20260918", 540 + i * 3) for i in reversed(range(130))]
    older = [raw("20260917", 750 + i * 3) for i in reversed(range(60))]
    responses = iter([Response(latest[:60], True), Response(latest[60:] + older, False)])
    monkeypatch.setattr(kw, "_post", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(time, "sleep", lambda _: None)
    result = kw.fetch_min_bars("token", "001520", 3, today=None, lookback_extra=60)
    assert len([row for row in result if row["_date"] == "20260918"]) == 130
    assert result[-1]["_date"] == "20260918"


def test_include_today_shows_running_session(monkeypatch):
    previous = [bar("2026-09-17", 540 + i * 3) for i in range(130)]
    today = [bar("2026-09-18", 540 + i * 3) for i in range(10)]
    monkeypatch.setattr(main, "_hub", lambda account: SimpleNamespace(
        data=SimpleNamespace(get_bars=lambda code, interval, lookback: previous + today)))
    monkeypatch.setattr(main, "now_kst", lambda: datetime(2026, 9, 18, 10, 0))
    result = main.get_bars("real", "001520", 3, 60, True, include_today=True)
    assert result["session_date"] == "2026-09-18"
    assert result["day_start_index"] == 60
    assert len(result["bars"]) == 70


def test_regular_session_keeps_closing_auction_bar(monkeypatch):
    day = [bar("2026-09-18", 540 + i * 3) for i in range(130)]  # 09:00~15:27
    closing = bar("2026-09-18", 930)                              # 15:30 종가 단일가
    after = [bar("2026-09-18", 933), bar("2026-09-18", 19 * 60 + 57)]
    monkeypatch.setattr(main, "_hub", lambda account: SimpleNamespace(
        data=SimpleNamespace(get_bars=lambda code, interval, lookback: day + [closing] + after)))
    monkeypatch.setattr(main, "now_kst", lambda: datetime(2026, 9, 18, 23, 0))
    result = main.get_bars("real", "338220", 3, 60, True)
    assert result["bars"][-1]["time"] == closing.time


def test_extended_uses_integrated_code_and_keeps_pre_and_after_market(monkeypatch):
    pre = [bar("2026-09-18", 480 + i * 3) for i in range(17)]     # 08:00~08:48 NXT 프리마켓
    regular = [bar("2026-09-18", 540 + i * 3) for i in range(131)]
    after = [bar("2026-09-18", 933 + i * 3) for i in range(89)]    # ~19:57
    calls = []

    def get_bars(code, interval, lookback):
        calls.append(code)
        return pre + regular + after

    monkeypatch.setattr(main, "_hub", lambda account: SimpleNamespace(data=SimpleNamespace(
        get_bars=get_bars, integrated_code=lambda code: f"{code}_AL")))
    monkeypatch.setattr(main, "now_kst", lambda: datetime(2026, 9, 18, 23, 0))
    result = main.get_bars("real", "338220", 3, 60, True, extended=True)
    assert calls == ["338220_AL"]
    assert len(result["bars"]) == 17 + 131 + 89
    assert result["bars"][-1]["time"] == after[-1].time

    # 일봉은 통합 코드로 바꾸지 않는다.
    calls.clear()
    monkeypatch.setattr(main, "_hub", lambda account: SimpleNamespace(data=SimpleNamespace(
        get_bars=get_bars, integrated_code=lambda code: f"{code}_AL",
        last_tick=lambda code: None)))
    main.get_bars("real", "338220", 1440, 60, False, extended=True)
    assert calls == ["338220"]


def day_bar(day):
    dt = datetime.strptime(day, "%Y-%m-%d")
    return Bar(time=int(dt.replace(tzinfo=timezone.utc).timestamp()), open=100,
               high=110, low=90, close=100, volume=10)


def weekdays(start, count):
    out, d = [], datetime.strptime(start, "%Y-%m-%d")
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d = datetime.fromordinal(d.toordinal() + 1)
    return out


def test_daily_anchor_shows_20_days_before_query_date_and_60_in_total(monkeypatch):
    """스크리너 조회일(anchor) 앞 20거래일부터 60거래일을 보여준다(맥락 + 전개)."""
    days = weekdays("2025-03-03", 460)          # 2025-03 ~ 2026-12 초(조회일 앞 MA 계산분 충분)
    bars = [day_bar(d) for d in days]
    calls = []

    def get_bars(code, interval, lookback):
        calls.append(lookback)
        return bars[-(lookback + 1):]

    monkeypatch.setattr(main, "_hub", lambda account: SimpleNamespace(
        data=SimpleNamespace(get_bars=get_bars, last_tick=lambda code: None)))
    monkeypatch.setattr(main, "now_kst", lambda: datetime(2026, 12, 10, 16, 0))
    anchor = "2026-07-01"
    result = main.get_bars("real", "014950", 1440, 119, False, False, False, anchor)
    shown = result["bars"][result["day_start_index"]:]
    shown_days = [datetime.fromtimestamp(b["time"], timezone.utc).date().isoformat() for b in shown]
    assert shown_days.index(anchor) == 20         # 조회일 앞 20거래일
    assert len(shown) == 60
    assert result["day_start_index"] == 119      # 그 앞은 MA 계산용
    assert calls[0] > 139                         # 조회일 앞 맥락까지 닿도록 더 받는다


def test_daily_anchor_recent_date_falls_back_to_latest_60(monkeypatch):
    """조회일 뒤로 60거래일이 아직 없으면 최근 60거래일 — 조회일은 그 안에 보인다."""
    days = weekdays("2026-01-01", 200)
    bars = [day_bar(d) for d in days]
    monkeypatch.setattr(main, "_hub", lambda account: SimpleNamespace(
        data=SimpleNamespace(get_bars=lambda c, i, n: bars[-(n + 1):], last_tick=lambda code: None)))
    last = days[-1]
    monkeypatch.setattr(main, "now_kst", lambda: datetime.strptime(last, "%Y-%m-%d").replace(hour=16))
    anchor = days[-10]
    result = main.get_bars("real", "014950", 1440, 119, False, False, False, anchor)
    shown = result["bars"][result["day_start_index"]:]
    shown_days = [datetime.fromtimestamp(b["time"], timezone.utc).date().isoformat() for b in shown]
    assert len(shown) == 60
    assert shown_days[-1] == last and anchor in shown_days


def test_daily_without_anchor_keeps_latest_60(monkeypatch):
    bars = [day_bar(d) for d in weekdays("2026-01-01", 120)]
    monkeypatch.setattr(main, "_hub", lambda account: SimpleNamespace(
        data=SimpleNamespace(get_bars=lambda c, i, n: bars, last_tick=lambda code: None)))
    result = main.get_bars("real", "014950", 1440, 119, False, False, False)
    assert result["day_start_index"] == 60 and len(result["bars"]) == 120
