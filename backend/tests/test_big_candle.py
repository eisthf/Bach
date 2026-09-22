"""거래대금 상위 양봉 조회: 키움 응답 결합, 스크리닝 조건, 당일/과거 라우팅."""
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import main
from app.providers import kiwoom_api as kw
from app.providers.krx_api import DailyQuote
from app.screener import (
    ScreenerError,
    screen_big_candles,
    screen_current_big_candles,
)

EOK = 100_000_000


# ---------------------------------------------------------------------------
# 키움 ka10032 + ka10028 결합
# ---------------------------------------------------------------------------
class Resp:
    status_code = 200

    def __init__(self, payload, cont=False):
        self.payload = payload
        self.headers = {"cont-yn": "Y" if cont else "N", "next-key": "k" if cont else ""}

    def json(self):
        return self.payload


def ranked(code, name, amount_mil, qty="1000"):
    return {"stk_cd": code, "stk_nm": name, "cur_prc": "+1", "trde_prica": str(amount_mil),
            "now_trde_qty": qty}


def rising(code, open_, cur, pre, flu="+1.00"):
    return {"stk_cd": code, "stk_nm": "", "open_pric": open_, "cur_prc": cur,
            "open_pric_pre": pre, "flu_rt": flu}


def test_fetch_big_candles_joins_amount_with_open(monkeypatch):
    calls = []

    def post(url, headers, body, **kwargs):
        api = headers["api-id"]
        calls.append((api, headers.get("next-key")))
        if api == "ka10032":
            assert body["stex_tp"] == "1"
            if headers.get("next-key") is None:
                return Resp({"return_code": 0, "trde_prica_upper": [
                    ranked("A000001", "대형양봉", 50_000, "123"),
                    ranked("000002", "대형음봉", 40_000),
                ]}, cont=True)
            return Resp({"return_code": 0, "trde_prica_upper": [
                ranked("000003", "경계양봉", 15_000),
                ranked("000004", "기준미달", 14_999),  # 여기서 연속조회를 멈춘다
            ]}, cont=True)
        assert api == "ka10028"
        assert body["trde_prica_cnd"] == "1000" and body["flu_cnd"] == "1"
        return Resp({"return_code": 0, "open_pric_pre_flu_rt": [
            rising("000001", "+10000", "+11000", "+10.00", "+12.30"),
            rising("000003", "-5000", "+5050", "+1.00", "-0.50"),
            rising("000004", "+100", "+120", "+20.00"),
            rising("000009", "+100", "+100", "0.00"),
        ]})

    monkeypatch.setattr(kw, "_post", post)
    got = kw.fetch_big_candles("t", 150 * EOK)
    assert got["scanned"] == 3  # 대형양봉·대형음봉·경계양봉 (기준미달 제외)
    assert got["stocks"] == [
        {"code": "000001", "name": "대형양봉", "open": 10000, "price": 11000,
         "change_pct": 12.3, "volume": 123, "amount": 50_000 * 1_000_000},
        {"code": "000003", "name": "경계양봉", "open": 5000, "price": 5050,
         "change_pct": -0.5, "volume": 1000, "amount": 15_000 * 1_000_000},
    ]
    # ka10032는 기준 미만이 보인 페이지에서 멈춘다(2페이지), ka10028은 1페이지.
    assert [c[0] for c in calls] == ["ka10032", "ka10032", "ka10028"]


def test_fetch_big_candles_failure_is_none(monkeypatch):
    def post(*args, **kwargs):
        raise kw.KiwoomRequestError("ka10032: 요청 실패")

    monkeypatch.setattr(kw, "_post", post)
    assert kw.fetch_big_candles("t", 150 * EOK) is None


def test_fetch_big_candles_propagates_auth_error(monkeypatch):
    def post(*args, **kwargs):
        raise kw.KiwoomAuthError("ka10032: 토큰 인증 거부")

    monkeypatch.setattr(kw, "_post", post)
    with pytest.raises(kw.KiwoomAuthError):
        kw.fetch_big_candles("t", 150 * EOK)


# ---------------------------------------------------------------------------
# 스크리닝 조건 (KRX 확정 일별)
# ---------------------------------------------------------------------------
def q(code, open_, close, amount, *, market="KOSPI"):
    return DailyQuote(code=code, name=f"종목{code}", market=market, open=open_, high=close,
                      low=open_, close=close, volume=10, market_cap=1, listed_shares=1,
                      amount=amount)


def fetcher(by_date):
    return lambda d: by_date.get(d, [])


def test_screen_big_candles_filters_amount_bullish_and_rise():
    get = fetcher({
        "20260918": [q("A", 1000, 1000, 0), q("B", 1000, 1000, 0), q("C", 1000, 1000, 0)],
        "20260921": [
            q("A", 1000, 1100, 200 * EOK),   # +10% 통과
            q("B", 1000, 1030, 150 * EOK),   # +3% 경계 거래대금 통과
            q("C", 1000, 1020, 149 * EOK),   # 거래대금 미달
            q("D", 1000, 990, 500 * EOK),    # 음봉
            q("E", 1000, 1000, 500 * EOK),   # 보합
        ],
    })
    res = screen_big_candles("2026-09-21", 0, 150 * EOK, fetch=get, source="krx")
    assert [s.code for s in res.stocks] == ["A", "B"]
    assert res.scanned == 4  # 거래대금 ≥150억: A·B·D·E (양봉 여부 무관)
    assert res.stocks[0].change_pct == 10.0


def test_screen_big_candles_scanned_counts_all_heavy_stocks():
    get = fetcher({"20260921": [q("A", 1000, 1100, 200 * EOK), q("D", 1000, 990, 500 * EOK),
                                q("C", 1000, 1020, 149 * EOK)]})
    res = screen_big_candles("2026-09-21", 0, 150 * EOK, fetch=get, source="krx")
    assert res.scanned == 2
    assert [s.code for s in res.stocks] == ["A"]
    assert res.stocks[0].change_pct is None  # 직전 거래일 자료가 없으면 None


def test_screen_big_candles_min_rise_boundary_and_sort():
    get = fetcher({
        "20260918": [q("A", 1, 1000, 0), q("B", 1, 1000, 0)],
        "20260921": [q("A", 1000, 1050, 150 * EOK), q("B", 1000, 1049, 900 * EOK),
                     q("C", 1000, 1200, 150 * EOK)],
    })
    res = screen_big_candles("2026-09-21", 5.0, 150 * EOK, fetch=get, source="krx")
    assert [(s.code, s.rise_pct) for s in res.stocks] == [("C", 20.0), ("A", 5.0)]
    assert res.stocks[1].change_pct == 5.0


def test_screen_big_candles_rejects_negative_rise():
    with pytest.raises(ScreenerError):
        screen_big_candles("2026-09-21", -1, 150 * EOK, fetch=fetcher({}), source="krx")


def test_mock_source_has_amount():
    res = screen_big_candles(None, 0, 150 * EOK, source="mock")
    assert res.source == "mock"
    assert res.stocks and all(s.amount >= 150 * EOK and s.close > s.open for s in res.stocks)


# ---------------------------------------------------------------------------
# 당일 키움 스냅샷
# ---------------------------------------------------------------------------
def test_current_big_candles_filters_and_enriches():
    current = {"scanned": 4, "stocks": [
        {"code": "A", "name": "에이", "open": 1000, "price": 1080, "change_pct": 3.0,
         "volume": 5, "amount": 300 * EOK},
        {"code": "B", "name": "비", "open": 1000, "price": 1010, "change_pct": None,
         "volume": 5, "amount": 300 * EOK},
    ]}
    meta = fetcher({"20260921": [DailyQuote(code="A", name="에이", market="KOSDAQ", open=1,
                                            high=1, low=1, close=1, volume=1, market_cap=1,
                                            listed_shares=10)]})
    res = screen_current_big_candles(current, 2.0, 150 * EOK, closed=False,
                                     captured_at="2026-09-22T10:00:00+09:00",
                                     fetch=meta, today=date(2026, 9, 22))
    assert res.snapshot and res.source == "kiwoom" and not res.closed
    assert res.scanned == 4 and res.date == "2026-09-22"
    assert [s.code for s in res.stocks] == ["A"]
    assert res.stocks[0].market == "KOSDAQ" and res.stocks[0].market_cap == 10 * 1080
    assert res.stocks[0].rise_pct == 8.0


def test_current_big_candles_survive_metadata_failure():
    def broken(_):
        raise RuntimeError("KRX down")

    current = {"scanned": 1, "stocks": [
        {"code": "A", "open": 1000, "price": 1100, "amount": 200 * EOK}]}
    res = screen_current_big_candles(current, 0, 150 * EOK, closed=True, fetch=broken,
                                     today=date(2026, 9, 22))
    assert [s.code for s in res.stocks] == ["A"] and res.stocks[0].market == ""


# ---------------------------------------------------------------------------
# 라우팅: 오늘 세션만 키움, 그 외는 KRX
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("at, requested, use_live, closed", [
    (datetime(2026, 9, 22, 10), None, True, False),
    (datetime(2026, 9, 22, 16), None, True, True),
    (datetime(2026, 9, 22, 16), "2026-09-22", True, True),
    (datetime(2026, 9, 22, 8, 30), None, False, None),        # 장전 → 전 거래일(KRX)
    (datetime(2026, 9, 22, 10), "2026-09-21", False, None),   # 과거일 → KRX
])
async def test_big_candle_routing(monkeypatch, at, requested, use_live, closed):
    live = Mock(return_value={"scanned": 0, "stocks": []})
    current = Mock(return_value="current")
    historical = Mock(return_value="historical")
    monkeypatch.setattr(main, "now_kst", lambda: at)
    monkeypatch.setattr(main, "manager", SimpleNamespace(
        configs=[SimpleNamespace(id="real", provider="kiwoom", kiwoom_mock=False)],
        hubs={"real": SimpleNamespace(data=SimpleNamespace(current_big_candles=live))},
    ))
    monkeypatch.setattr(main, "screen_current_big_candles", current)
    monkeypatch.setattr(main, "screen_big_candles", historical)

    result = await main.screener_big_candle(date=requested, min_rise_pct=1.5, min_amount_eok=150)

    assert result == ("current" if use_live else "historical")
    if use_live:
        live.assert_called_once_with(150 * EOK)
        assert current.call_args.kwargs["closed"] is closed
    else:
        live.assert_not_called()
        historical.assert_called_once_with(requested, 1.5, 150 * EOK)


async def test_big_candle_live_failure_is_502(monkeypatch):
    monkeypatch.setattr(main, "now_kst", lambda: datetime(2026, 9, 22, 10))
    monkeypatch.setattr(main, "manager", SimpleNamespace(
        configs=[SimpleNamespace(id="real", provider="kiwoom", kiwoom_mock=False)],
        hubs={"real": SimpleNamespace(data=SimpleNamespace(
            current_big_candles=Mock(return_value=None)))},
    ))
    with pytest.raises(main.HTTPException) as e:
        await main.screener_big_candle(date=None, min_rise_pct=0, min_amount_eok=150)
    assert e.value.status_code == 502
