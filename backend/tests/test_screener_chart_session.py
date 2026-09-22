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
