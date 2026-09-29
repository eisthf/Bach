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
    # 15:35는 정규장 목록(KRX, call(True))과 통합 결과(call())를 함께 수집한다.
    assert [c.args for c in current.call_args_list] == [(True,), ()]
    assert load_snapshot("2026-09-21", 29, 30, "regular") is not None
    # 새 호출도 디스크를 확인하므로 서버 재시작에 무관하게 중복 수집하지 않는다.
    await collector.collect_once(manager)
    assert current.call_count == 2


@pytest.mark.parametrize("at", [
    datetime(2026, 9, 21, 15, 30, tzinfo=KST),
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
    def cross_midnight(*_):
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


async def test_regular_window_saves_only_krx_list(setup):
    clock, current, manager, result = setup
    clock[0] = datetime(2026, 9, 21, 15, 31, tzinfo=KST)
    await collector.collect_once(manager)
    assert [c.args for c in current.call_args_list] == [(True,)]  # 15:35 전: 정규장 목록만
    assert load_snapshot("2026-09-21", 29, 30, "regular") is not None
    assert load_snapshot("2026-09-21", 29, 30) is None


async def test_final_collection_after_nxt_close(setup):
    clock, current, manager, result = setup
    save_snapshot(result.model_copy(), datetime(2026, 9, 21, 15, 36, tzinfo=KST))
    clock[0] = datetime(2026, 9, 21, 20, 6, tzinfo=KST)
    await collector.collect_once(manager)       # 15:36 저장분은 최종이 아니므로 다시 수집
    assert load_snapshot("2026-09-21", 29, 30).captured_at.startswith("2026-09-21T20:06")
    await collector.collect_once(manager)
    assert current.call_count == 1


def test_live_result_appends_regular_limit_that_fell_after_hours(monkeypatch, tmp_path):
    from app.models import UpperLimitStock
    monkeypatch.setenv("SCREENER_CACHE_DIR", str(tmp_path))
    base = dict(market="KOSDAQ", volume=1, market_cap=1)
    regular = UpperLimitResult(date="2026-09-21", prev_date="2026-09-18", min_pct=29, max_pct=30,
                               source="kiwoom", snapshot=True, scanned=2, stocks=[
        UpperLimitStock(code="000001", name="유지", close=1300, prev_close=1000, change_pct=30.0, **base),
        UpperLimitStock(code="000002", name="이탈", close=1300, prev_close=1000, change_pct=30.0, **base),
    ])
    save_snapshot(regular, datetime(2026, 9, 21, 15, 31, tzinfo=KST), "regular")
    latest = regular.model_copy(update={"stocks": [regular.stocks[0]]})
    monkeypatch.setattr(collector, "screen_current_upper_limits", lambda *a, **k: latest)
    data = SimpleNamespace(current_upper_limits=lambda: [], last_price=lambda code: 1150)
    got = collector.live_upper_limits(data, 29, 30, datetime(2026, 9, 21, 20, 6, tzinfo=KST))
    drop = {s.code: s for s in got.stocks}["000002"]
    assert drop.after_hours_drop and drop.close == 1150 and drop.change_pct == 15.0
    assert drop.regular_close == 1300
