"""일봉 조회의 mock/live provider 정합성 검증."""
from types import SimpleNamespace

from app.providers import kiwoom_api as kw
from app.providers.base import DAY_INTERVAL, VALID_INTERVALS
from app.providers.kiwoom import KiwoomDataProvider
from app.providers.mock import MockDataProvider


def test_day_interval_is_supported():
    assert DAY_INTERVAL == 1440
    assert DAY_INTERVAL in VALID_INTERVALS


def test_mock_day_bars_are_ordered_weekdays():
    bars = MockDataProvider().get_bars("005930", DAY_INTERVAL, 60)
    assert len(bars) >= 60
    assert [bar.time for bar in bars] == sorted(bar.time for bar in bars)
    assert len({bar.time for bar in bars}) == len(bars)
    # 차트 축에서 일봉 시각은 각 KST 날짜의 00:00이다.
    from datetime import datetime, timezone
    for bar in bars:
        assert datetime.fromtimestamp(bar.time, timezone.utc).weekday() < 5


def test_mock_fetches_enough_warmup_for_sixty_visible_ma_points():
    bars = MockDataProvider().get_bars("005930", DAY_INTERVAL, 119)
    assert len(bars) == 120
    # 120개에서 MA60은 61개 점이 생겨 최근 60일 전체에 선을 그릴 수 있다.
    assert len(bars) - 60 + 1 == 61


def test_fetch_day_bars_pages_normalizes_and_deduplicates(monkeypatch):
    pages = [
        SimpleNamespace(
            status_code=200,
            headers={"cont-yn": "Y", "next-key": "next"},
            json=lambda: {"return_code": 0, "stk_dt_pole_chart_qry": [
                {"dt": "20260904", "open_pric": "100", "high_pric": "120",
                 "low_pric": "90", "cur_prc": "110", "trde_qty": "1000"},
                {"dt": "20260903", "open_pric": "90", "high_pric": "105",
                 "low_pric": "80", "cur_prc": "100", "trde_qty": "900"},
            ]},
        ),
        SimpleNamespace(
            status_code=200,
            headers={"cont-yn": "N", "next-key": ""},
            json=lambda: {"return_code": 0, "stk_dt_pole_chart_qry": [
                {"dt": "20260903", "open_pric": "90", "high_pric": "105",
                 "low_pric": "80", "cur_prc": "100", "trde_qty": "900"},
                {"dt": "20260902", "open_pric": "80", "high_pric": "95",
                 "low_pric": "75", "cur_prc": "90", "trde_qty": "800"},
            ]},
        ),
    ]
    monkeypatch.setattr(kw, "_post", lambda *args, **kwargs: pages.pop(0))
    monkeypatch.setattr("time.sleep", lambda *_: None)

    rows = kw.fetch_day_bars("token", "005930", today="20260904", lookback_extra=2)
    assert [row["_date"] for row in rows] == ["20260902", "20260903", "20260904"]
    assert rows[-1]["close"] == 110
    assert rows[-1]["volume"] == 1000


def test_kiwoom_provider_routes_daily_requests_to_ka10081(monkeypatch):
    provider = KiwoomDataProvider.__new__(KiwoomDataProvider)
    provider.token = "token"
    provider._mock = False
    provider._day_open = {}
    provider._day_open_day = {}
    day_calls = []
    monkeypatch.setattr(kw, "fetch_day_bars", lambda *args, **kwargs: day_calls.append(kwargs) or [{
        "time": 1, "open": 10, "high": 12, "low": 9, "close": 11,
        "volume": 100, "_date": "20260904",
    }])
    monkeypatch.setattr(kw, "fetch_min_bars",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()))

    bars = provider._fetch_bars("005930", DAY_INTERVAL, 60)
    assert len(bars) == 1
    assert bars[0].close == 11
    assert day_calls[0]["lookback_extra"] == 60
