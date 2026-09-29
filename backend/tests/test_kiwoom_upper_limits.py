"""키움 ka10017 당일 상한가 응답 정규화."""
from types import SimpleNamespace

from app.providers import kiwoom_api as kw


def response(payload, status=200):
    return SimpleNamespace(status_code=status, json=lambda: payload)


def test_fetch_upper_limits_normalizes_signed_prices(monkeypatch):
    def post(url, headers, body, **kwargs):
        assert headers["api-id"] == "ka10017"
        assert body["mrkt_tp"] == "000"
        assert body["updown_tp"] == "1"
        assert body["stex_tp"] == "3"  # 통합(KRX+NXT): 시간외 상한가도 잡는다
        return response({
            "return_code": 0,
            "updown_pric": [{
                "stk_cd": "048770_AL", "stk_nm": "TPC로보틱스",
                "cur_prc": "+003965", "pred_pre": "+000915", "flu_rt": "+30.00",
                "trde_qty": "12,345,678",
            }],
        })

    monkeypatch.setattr(kw, "_post", post)
    assert kw.fetch_upper_limits("token") == [{
        "code": "048770", "name": "TPC로보틱스", "price": 3965,
        "prev_close": 3050, "change_pct": 30.0, "volume": 12_345_678,
    }]


def test_fetch_upper_limits_distinguishes_empty_from_failure(monkeypatch):
    monkeypatch.setattr(kw, "_post", lambda *args, **kwargs: response({
        "return_code": 0, "updown_pric": [],
    }))
    assert kw.fetch_upper_limits("token") == []
    monkeypatch.setattr(kw, "_post", lambda *args, **kwargs: response({}, status=500))
    assert kw.fetch_upper_limits("token") is None


def test_base_code_strips_exchange_suffix_and_prefix():
    assert [kw.base_code(c) for c in ("A338220", "338220_AL", "338220_NX", " 0197X0 ")] == \
        ["338220", "338220", "338220", "0197X0"]


def _provider(monkeypatch, now, bars):
    from app.providers import kiwoom as module
    monkeypatch.setattr(kw, "fetch_access_token", lambda *a, **k: kw.AccessToken("t", "20990101000000"))
    monkeypatch.setattr(module, "now_kst", lambda: now)
    monkeypatch.setattr(kw, "fetch_upper_limits", lambda *a, **k: [
        {"code": "338220", "name": "뷰노", "price": 7240, "prev_close": 5570,
         "change_pct": 29.98, "volume": 5_194_231}])
    provider = module.KiwoomDataProvider("t", "t", mock=True)
    calls = []

    def get_bars(code, interval, lookback):
        calls.append((code, interval, lookback))
        return bars
    monkeypatch.setattr(provider, "get_bars", get_bars)
    return provider, calls


def _bar(hhmm, close, day="2026-09-28"):
    from datetime import datetime, timezone
    from app.models import Bar
    dt = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    return Bar(time=int(dt.timestamp()), open=close, high=close, low=close, close=close, volume=1)


def test_after_close_attaches_regular_session_close(monkeypatch):
    from datetime import datetime
    from app.market_clock import KST
    bars = [_bar("15:27", 6860, "2026-09-25"), _bar("09:00", 6520), _bar("15:18", 6860),
            _bar("15:30", 6820), _bar("15:33", 6820), _bar("19:57", 7240)]
    provider, calls = _provider(monkeypatch, datetime(2026, 9, 28, 23, 25, tzinfo=KST), bars)
    rows = provider.current_upper_limits()
    assert rows[0]["regular_close"] == 6820  # 15:30 종가 단일가 봉, 시간외 7,240 아님
    assert calls == [("338220", 3, 0)]       # KRX 코드로 정규장 봉을 본다


def test_during_regular_session_skips_regular_close_lookup(monkeypatch):
    from datetime import datetime
    from app.market_clock import KST
    provider, calls = _provider(monkeypatch, datetime(2026, 9, 28, 11, 0, tzinfo=KST), [])
    rows = provider.current_upper_limits()
    assert "regular_close" not in rows[0] and calls == []
