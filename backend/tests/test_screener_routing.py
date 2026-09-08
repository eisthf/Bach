"""당일 스냅샷을 전 거래일 결과로 잘못 표시하지 않는지 검증한다."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("at, requested, use_snapshot", [
    (datetime(2026, 9, 8, 8, 55), "2026-09-07", False),
    (datetime(2026, 9, 8, 8, 55), None, False),
    (datetime(2026, 9, 7, 8, 55), None, False),
    (datetime(2026, 9, 6, 12), None, False),
    (datetime(2026, 9, 8, 9), None, True),
    (datetime(2026, 9, 8, 16), "2026-09-08", True),
    (datetime(2026, 9, 8, 12), "2026-09-07", False),
])
async def test_snapshot_only_for_current_calendar_session(
    monkeypatch, at, requested, use_snapshot,
):
    monkeypatch.setenv("ACCOUNTS", "mock")
    monkeypatch.setenv("MOCK_PROVIDER", "mock")
    from app import main

    current = Mock(return_value=[])
    snapshot = Mock(return_value="snapshot")
    historical = Mock(return_value="historical")
    monkeypatch.setattr(main, "now_kst", lambda: at)
    monkeypatch.setattr(main, "source_name", lambda: "krx")
    monkeypatch.setattr(main, "manager", SimpleNamespace(
        configs=[SimpleNamespace(id="real", provider="kiwoom", kiwoom_mock=False)],
        hubs={"real": SimpleNamespace(data=SimpleNamespace(current_upper_limits=current))},
    ))
    monkeypatch.setattr(main, "screen_current_upper_limits", snapshot)
    monkeypatch.setattr(main, "screen_upper_limit", historical)

    result = await main.screener_upper_limit(date=requested, min_pct=29, max_pct=30)

    assert result == ("snapshot" if use_snapshot else "historical")
    if use_snapshot:
        snapshot.assert_called_once_with([], 29, 30, today=at.date())
        historical.assert_not_called()
    else:
        historical.assert_called_once_with(requested, 29, 30)
        current.assert_not_called()
