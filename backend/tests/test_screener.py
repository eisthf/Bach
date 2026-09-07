"""상한가 스크리너 회귀 테스트.

전종목 시세 fetch 를 주입해 네트워크 없이 판정 로직만 검증한다.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.providers.krx_api import DailyQuote, parse_rows
from app.screener import (
    NotATradingDay,
    ScreenerError,
    _mock_quotes,
    latest_session_date,
    screen_current_upper_limits,
    screen_upper_limit,
)


def q(code: str, close: int, *, name: str = "", cap: int = 0) -> DailyQuote:
    return DailyQuote(
        code=code, name=name or f"종목{code}", market="KOSDAQ",
        open=close, high=close, low=close, close=close,
        volume=1000, market_cap=cap or close * 1000, listed_shares=1000,
    )


def fetcher(by_date: dict[str, list[DailyQuote]]):
    """날짜별 시세 딕셔너리를 fetch 콜러블로. 없는 날짜는 휴장(빈 리스트)."""
    return lambda d: by_date.get(d, [])


# ---------------------------------------------------------------------------
# 판정 구간
# ---------------------------------------------------------------------------
def test_band_boundaries_are_inclusive():
    """29%·30% 정확히 걸친 종목은 포함, 바깥은 제외."""
    prev = [q(c, 10_000) for c in ("A", "B", "C", "D", "E")]
    day = [
        q("A", 12_890),   # +28.90% → 제외
        q("B", 12_900),   # +29.00% → 포함(경계)
        q("C", 12_950),   # +29.50% → 포함
        q("D", 13_000),   # +30.00% → 포함(경계)
        q("E", 13_010),   # +30.10% → 제외
    ]
    res = screen_upper_limit(
        date="2026-03-10",
        fetch=fetcher({"20260310": day, "20260309": prev}),
        source="test",
    )
    assert [s.code for s in res.stocks] == ["D", "C", "B"]
    assert res.scanned == 5
    assert res.date == "2026-03-10"
    assert res.prev_date == "2026-03-09"


def test_sorted_by_change_pct_desc():
    prev = [q("A", 1_000), q("B", 1_000), q("C", 1_000)]
    day = [q("A", 1_292), q("B", 1_299), q("C", 1_295)]
    res = screen_upper_limit(
        date="20260310",
        fetch=fetcher({"20260310": day, "20260309": prev}),
        source="test",
    )
    assert [s.code for s in res.stocks] == ["B", "C", "A"]
    assert res.stocks[0].change_pct == 29.9


def test_custom_band():
    prev = [q("A", 1_000), q("B", 1_000)]
    day = [q("A", 1_100), q("B", 1_295)]
    res = screen_upper_limit(
        date="20260310", min_pct=5, max_pct=15,
        fetch=fetcher({"20260310": day, "20260309": prev}),
        source="test",
    )
    assert [s.code for s in res.stocks] == ["A"]


def test_min_greater_than_max_rejected():
    with pytest.raises(ScreenerError):
        screen_upper_limit(date="20260310", min_pct=30, max_pct=29,
                           fetch=fetcher({}), source="test")


# ---------------------------------------------------------------------------
# 거래일 판정
# ---------------------------------------------------------------------------
def test_prev_trading_day_skips_holidays():
    """D-1이 휴장이면 시세가 있는 날까지 거슬러 올라간다(달력상 전날 아님)."""
    prev = [q("A", 10_000)]
    day = [q("A", 12_950)]
    # 2026-03-10(화)의 직전 거래일이 03-06(금) — 사이 나흘이 모두 휴장.
    res = screen_upper_limit(
        date="20260310",
        fetch=fetcher({"20260310": day, "20260306": prev}),
        source="test",
    )
    assert res.prev_date == "2026-03-06"
    assert [s.code for s in res.stocks] == ["A"]


def test_default_date_is_latest_trading_day(monkeypatch):
    """날짜 생략 시 오늘부터 거슬러 시세가 있는 첫 날을 D로 잡는다."""
    import app.screener as screener
    from datetime import datetime

    class FakeNow:
        @staticmethod
        def date():
            return datetime(2026, 3, 15).date()   # 일요일

    monkeypatch.setattr(screener, "now_kst", lambda: FakeNow)
    res = screen_upper_limit(
        fetch=fetcher({"20260313": [q("A", 12_950)], "20260312": [q("A", 10_000)]}),
        source="test",
    )
    assert res.date == "2026-03-13"       # 금요일로 내려감
    assert res.prev_date == "2026-03-12"


def test_latest_session_date_keeps_previous_day_before_open():
    from datetime import datetime

    assert latest_session_date(datetime(2026, 9, 8, 0, 14)) == date(2026, 9, 7)
    assert latest_session_date(datetime(2026, 9, 8, 8, 59)) == date(2026, 9, 7)
    assert latest_session_date(datetime(2026, 9, 8, 9, 0)) == date(2026, 9, 8)


def test_latest_session_date_skips_weekend_before_monday_open():
    from datetime import datetime

    assert latest_session_date(datetime(2026, 9, 7, 8, 0)) == date(2026, 9, 4)
    assert latest_session_date(datetime(2026, 9, 6, 12, 0)) == date(2026, 9, 4)


def test_current_upper_limits_use_today_and_previous_krx_metadata():
    previous = [q("048770", 3_050, name="TPC로보틱스", cap=46_000_000_000)]
    res = screen_current_upper_limits(
        [{"code": "048770", "name": "TPC로보틱스", "price": 3_965,
          "prev_close": 3_050, "change_pct": 30.0, "volume": 12_345_678}],
        fetch=fetcher({"20260904": previous}),
        today=date(2026, 9, 7),
    )
    assert res.date == "2026-09-07"
    assert res.prev_date == "2026-09-04"
    assert res.source == "kiwoom"
    assert res.snapshot is True
    assert res.scanned == 1
    assert res.stocks[0].market == "KOSDAQ"
    assert res.stocks[0].market_cap == 3_965_000
    assert res.stocks[0].volume == 12_345_678


def test_current_upper_limits_keep_successful_empty_today():
    res = screen_current_upper_limits(
        [], fetch=fetcher({"20260904": [q("A", 1000)]}),
        today=date(2026, 9, 7),
    )
    assert res.date == "2026-09-07"
    assert res.stocks == []
    assert res.snapshot is True


def test_non_trading_day_raises():
    with pytest.raises(NotATradingDay):
        screen_upper_limit(date="20260315", fetch=fetcher({}), source="test")


def test_bad_date_format_raises():
    with pytest.raises(ScreenerError):
        screen_upper_limit(date="2026-13-99", fetch=fetcher({}), source="test")


# ---------------------------------------------------------------------------
# 데이터 결손
# ---------------------------------------------------------------------------
def test_stock_without_prev_close_is_skipped():
    """신규상장 등 전일 종가가 없는 종목은 등락률을 매길 수 없어 제외."""
    day = [q("NEW", 13_000), q("OLD", 12_950)]
    prev = [q("OLD", 10_000)]
    res = screen_upper_limit(
        date="20260310",
        fetch=fetcher({"20260310": day, "20260309": prev}),
        source="test",
    )
    assert [s.code for s in res.stocks] == ["OLD"]
    assert res.scanned == 2   # 훑은 수에는 포함


def test_zero_prev_close_is_skipped():
    res = screen_upper_limit(
        date="20260310",
        fetch=fetcher({"20260310": [q("A", 13_000)], "20260309": [q("A", 0)]}),
        source="test",
    )
    assert res.stocks == []


# ---------------------------------------------------------------------------
# 합성(mock) 소스
# ---------------------------------------------------------------------------
def test_mock_source_yields_hits_inside_band():
    res = screen_upper_limit(date="2026-03-10", source="mock")
    assert res.scanned > 0
    assert res.stocks, "합성 데이터에도 상한가 종목이 있어야 데모가 된다"
    for s in res.stocks:
        assert 29.0 <= s.change_pct <= 30.0
        assert s.market_cap > 0
        assert s.name


def test_mock_source_is_reproducible():
    a = screen_upper_limit(date="2026-03-10", source="mock")
    b = screen_upper_limit(date="2026-03-10", source="mock")
    assert [s.code for s in a.stocks] == [s.code for s in b.stocks]
    assert [s.close for s in a.stocks] == [s.close for s in b.stocks]


def test_mock_weekend_is_not_a_trading_day():
    assert _mock_quotes("20260314") == []   # 토요일
    assert _mock_quotes("20260315") == []   # 일요일


# ---------------------------------------------------------------------------
# KRX 응답 파싱
# ---------------------------------------------------------------------------
def test_parse_rows_handles_commas_and_isin():
    rows = [{
        "ISU_CD": "KR7005930003",       # ISIN → 단축코드 005930
        "ISU_ABBRV": "삼성전자",
        "TDD_OPNPRC": "70,000",
        "TDD_HGPRC": "72,500",
        "TDD_LWPRC": "69,800",
        "TDD_CLSPRC": "72,000",
        "ACC_TRDVOL": "12,345,678",
        "MKTCAP": "429,000,000,000,000",
        "LIST_SHRS": "5,969,782,550",
    }]
    (quote,) = parse_rows(rows, "KOSPI")
    assert quote.code == "005930"
    assert quote.name == "삼성전자"
    assert quote.close == 72_000
    assert quote.volume == 12_345_678
    assert quote.market_cap == 429_000_000_000_000


def test_parse_rows_falls_back_to_shares_times_close_for_market_cap():
    rows = [{
        "ISU_SRT_CD": "005930", "ISU_NM": "삼성전자",
        "TDD_CLSPRC": "1,000", "LIST_SHRS": "2,000", "MKTCAP": "",
    }]
    (quote,) = parse_rows(rows, "KOSPI")
    assert quote.market_cap == 2_000_000


def test_parse_rows_drops_rows_without_code():
    assert parse_rows([{"ISU_NM": "이름만"}, "쓰레기"], "KOSPI") == []
