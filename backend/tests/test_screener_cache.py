from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.market_clock import KST
from app.models import UpperLimitResult
from app.screener import DataPending
from app.screener_cache import load_snapshot, save_snapshot


@pytest.fixture
def cached(monkeypatch, tmp_path):
    monkeypatch.setenv("SCREENER_CACHE_DIR", str(tmp_path))
    result = UpperLimitResult(date="2026-09-21", prev_date="2026-09-18",
                              min_pct=29, max_pct=30, source="kiwoom",
                              snapshot=True, scanned=0, stocks=[])
    save_snapshot(result, datetime(2026, 9, 21, 23, 59, tzinfo=KST))
    return result


def test_persisted_snapshot_keeps_date_time_and_filter(cached):
    result = load_snapshot("2026-09-21", 29, 30)
    assert result.cached and result.snapshot
    assert result.captured_at == "2026-09-21T23:59:00+09:00"
    assert "확정 종가가 아니며" in result.notice
    assert result.stocks == []  # 0종목도 성공한 조회 결과
    assert load_snapshot("2026-09-22", 29, 30) is None
    assert load_snapshot("2026-09-21", 29.5, 30) is None
    save_snapshot(cached, datetime(2026, 9, 21, 12, tzinfo=KST))
    assert load_snapshot("2026-09-21", 29, 30).captured_at.startswith("2026-09-21T23:59")


@pytest.mark.parametrize("requested", ["2026-09-21", None])
async def test_midnight_fallback_and_krx_replacement(monkeypatch, cached, requested):
    from app import main
    monkeypatch.setattr(main, "now_kst", lambda: datetime(2026, 9, 22, 0, 1, tzinfo=KST))
    monkeypatch.setattr(main, "source_name", lambda: "krx")
    current = Mock(side_effect=AssertionError("과거 날짜를 당일 API로 조회하면 안 됨"))
    monkeypatch.setattr(main, "manager", SimpleNamespace(
        configs=[SimpleNamespace(id="real", provider="kiwoom", kiwoom_mock=False)],
        hubs={"real": SimpleNamespace(data=SimpleNamespace(current_upper_limits=current))},
    ))
    historical = Mock(side_effect=DataPending(datetime(2026, 9, 21).date()))
    monkeypatch.setattr(main, "screen_upper_limit", historical)
    result = await main.screener_upper_limit(requested, 29, 30)
    assert result.cached and result.date == "2026-09-21"
    historical.side_effect = None
    historical.return_value = cached.model_copy(update={"date": "2026-09-18", "source": "krx"})
    assert (await main.screener_upper_limit(requested, 29, 30)).cached
    historical.return_value = cached.model_copy(update={"source": "krx", "snapshot": False})
    result = await main.screener_upper_limit(requested, 29, 30)
    assert result.source == "krx" and not result.cached
    current.assert_not_called()


async def test_current_query_saves_for_next_day(monkeypatch, cached):
    from app import main
    at = datetime(2026, 9, 21, 23, 59, 30, tzinfo=KST)
    monkeypatch.setattr(main, "now_kst", lambda: at)
    monkeypatch.setattr(main, "source_name", lambda: "krx")
    monkeypatch.setattr(main, "manager", SimpleNamespace(
        configs=[SimpleNamespace(id="real", provider="kiwoom", kiwoom_mock=False)],
        hubs={"real": SimpleNamespace(data=SimpleNamespace(current_upper_limits=lambda: []))},
    ))
    monkeypatch.setattr(main, "screen_current_upper_limits", lambda *a, **kw: cached)
    await main.screener_upper_limit("2026-09-21", 29, 30)
    assert load_snapshot("2026-09-21", 29, 30).captured_at == at.isoformat()


def test_corrupt_cache_is_ignored_and_replaced(cached, tmp_path):
    path = next(tmp_path.glob("*.json"))
    path.write_text("broken", encoding="utf-8")
    assert load_snapshot("2026-09-21", 29, 30) is None
    save_snapshot(cached, datetime(2026, 9, 21, 23, 59, tzinfo=KST))
    assert load_snapshot("2026-09-21", 29, 30).cached


async def test_next_day_krx_result_keeps_after_hours_limit_stocks(monkeypatch, tmp_path):
    """KRX 확정 자료(정규장 종가)에는 없는 시간외 상한가 종목을 저장된 조회에서 덧붙인다."""
    from app import main
    from app.models import UpperLimitStock
    monkeypatch.setenv("SCREENER_CACHE_DIR", str(tmp_path))
    stock = dict(market="KOSDAQ", volume=1, market_cap=1)
    saved = UpperLimitResult(
        date="2026-09-28", prev_date="2026-09-23", min_pct=29, max_pct=30, source="kiwoom",
        snapshot=True, scanned=2, stocks=[
            UpperLimitStock(code="028300", name="HLB", close=39700, prev_close=30550,
                            change_pct=29.95, regular_close=39700, **stock),
            UpperLimitStock(code="338220", name="뷰노", close=7240, prev_close=5570,
                            change_pct=29.98, regular_close=6820, after_hours=True, **stock),
        ])
    save_snapshot(saved, datetime(2026, 9, 28, 23, 25, tzinfo=KST))
    krx = UpperLimitResult(
        date="2026-09-28", prev_date="2026-09-23", min_pct=29, max_pct=30, source="krx",
        scanned=2700, stocks=[UpperLimitStock(code="028300", name="HLB", close=39700,
                                              prev_close=30550, change_pct=29.95, **stock)])
    monkeypatch.setattr(main, "now_kst", lambda: datetime(2026, 9, 29, 8, 10, tzinfo=KST))
    monkeypatch.setattr(main, "source_name", lambda: "krx")
    monkeypatch.setattr(main, "screen_upper_limit", Mock(return_value=krx))
    result = await main.screener_upper_limit("2026-09-28", 29, 30)
    assert result.source == "krx"
    assert [(s.code, s.after_hours) for s in result.stocks] == [("028300", False), ("338220", True)]
    assert "시간외" in result.notice


def test_merge_marks_regular_limits_that_fell_after_hours():
    from app.main import merge_after_hours
    from app.models import UpperLimitStock
    base = dict(market="KOSDAQ", volume=1, market_cap=1, prev_close=1000)
    krx = UpperLimitResult(date="2026-09-28", prev_date="2026-09-23", min_pct=29, max_pct=30,
                           source="krx", scanned=9, stocks=[
        UpperLimitStock(code="A", name="유지", close=1300, change_pct=30.0, **base),
        UpperLimitStock(code="B", name="저장된 이탈", close=1300, change_pct=30.0, **base),
        UpperLimitStock(code="C", name="최종 조회에 없음", close=1300, change_pct=30.0, **base),
    ])
    saved = UpperLimitResult(date="2026-09-28", prev_date="2026-09-23", min_pct=29, max_pct=30,
                             source="kiwoom", snapshot=True, scanned=9,
                             captured_at="2026-09-28T20:06:00+09:00", stocks=[
        UpperLimitStock(code="A", name="유지", close=1300, change_pct=30.0, **base),
        UpperLimitStock(code="B", name="저장된 이탈", close=1150, change_pct=15.0,
                        regular_close=1300, after_hours_drop=True, **base),
        UpperLimitStock(code="D", name="시간외 상한가", close=1300, change_pct=30.0,
                        regular_close=1100, after_hours=True, **base),
    ])
    merge_after_hours(krx, saved)
    rows = {s.code: s for s in krx.stocks}
    assert not rows["A"].after_hours_drop and not rows["A"].after_hours
    assert rows["B"].after_hours_drop and rows["B"].close == 1150    # 저장된 마지막 거래가
    assert rows["C"].after_hours_drop and rows["C"].close == 1300    # 최종(20:05 이후) 조회에 없음
    assert rows["D"].after_hours
    assert "시간외" in krx.notice

    # 20:05 이전 저장분이면 '없음'을 이탈로 단정하지 않는다.
    early = saved.model_copy(update={"captured_at": "2026-09-28T16:00:00+09:00"})
    krx2 = krx.model_copy(update={"stocks": [UpperLimitStock(code="C", name="c", close=1300,
                                                             change_pct=30.0, **base)]})
    merge_after_hours(krx2, early)
    assert not {s.code: s for s in krx2.stocks}["C"].after_hours_drop
