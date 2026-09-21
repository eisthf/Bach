from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import screener_collector as collector
from app.market_clock import KST
from app.models import UpperLimitResult
from app.screener_cache import load_snapshot, save_snapshot


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("SCREENER_CACHE_DIR", str(tmp_path))
    clock = [datetime(2026, 9, 21, 15, 35, tzinfo=KST)]
    monkeypatch.setattr(collector, "now_kst", lambda: clock[0])
    monkeypatch.setattr(collector, "source_name", lambda: "krx")
    current = Mock(return_value=[])
    manager = SimpleNamespace(
        configs=[SimpleNamespace(id="live", provider="kiwoom", kiwoom_mock=False)],
        hubs={"live": SimpleNamespace(data=SimpleNamespace(current_upper_limits=current))},
    )
    result = UpperLimitResult(date="2026-09-21", prev_date="2026-09-18",
                              min_pct=29, max_pct=30, source="kiwoom",
                              snapshot=True, scanned=0)
    monkeypatch.setattr(collector, "screen_current_upper_limits", Mock(return_value=result))
    return clock, current, manager, result


async def test_collect_after_close_and_skip_persisted_result(setup):
    clock, current, manager, result = setup
    save_snapshot(result, clock[0].replace(hour=12))
    await collector.collect_once(manager)
    assert load_snapshot("2026-09-21", 29, 30).captured_at == clock[0].isoformat()
    # 새 호출도 디스크를 확인하므로 서버 재시작에 무관하게 중복 수집하지 않는다.
    await collector.collect_once(manager)
    current.assert_called_once()


@pytest.mark.parametrize("at", [
    datetime(2026, 9, 21, 15, 34, tzinfo=KST),
    datetime(2026, 9, 22, 0, 1, tzinfo=KST),
    datetime(2026, 9, 26, 16, tzinfo=KST),
])
async def test_no_collection_before_close_or_weekend(setup, at):
    clock, current, manager, _ = setup
    clock[0] = at
    await collector.collect_once(manager)
    current.assert_not_called()


async def test_late_start_and_retry_after_api_failure(setup):
    clock, current, manager, _ = setup
    clock[0] = clock[0].replace(hour=23)
    current.return_value = None
    with pytest.raises(RuntimeError):
        await collector.collect_once(manager)
    assert load_snapshot("2026-09-21", 29, 30) is None
    current.return_value = []
    await collector.collect_once(manager)
    assert load_snapshot("2026-09-21", 29, 30) is not None


async def test_midnight_response_is_not_saved(setup):
    clock, current, manager, _ = setup
    def cross_midnight():
        clock[0] = datetime(2026, 9, 22, 0, 0, tzinfo=KST)
        return []
    current.side_effect = cross_midnight
    await collector.collect_once(manager)
    assert load_snapshot("2026-09-21", 29, 30) is None


async def test_save_failure_can_retry(setup, monkeypatch):
    _, current, manager, _ = setup
    monkeypatch.setattr(collector, "save_snapshot", Mock(return_value=False))
    with pytest.raises(RuntimeError):
        await collector.collect_once(manager)
    with pytest.raises(RuntimeError):
        await collector.collect_once(manager)
    assert current.call_count == 2


async def test_mock_account_is_not_used(setup):
    _, current, manager, _ = setup
    manager.configs[0].kiwoom_mock = True
    await collector.collect_once(manager)
    current.assert_not_called()
