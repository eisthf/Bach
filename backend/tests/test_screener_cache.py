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
