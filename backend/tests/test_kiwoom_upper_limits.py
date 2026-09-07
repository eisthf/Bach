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
        assert body["stex_tp"] == "1"
        return response({
            "return_code": 0,
            "updown_pric": [{
                "stk_cd": "A048770", "stk_nm": "TPC로보틱스",
                "cur_prc": "+003965", "pred_pre": "+000915", "flu_rt": "+30.00",
            }],
        })

    monkeypatch.setattr(kw, "_post", post)
    assert kw.fetch_upper_limits("token") == [{
        "code": "048770", "name": "TPC로보틱스", "price": 3965,
        "prev_close": 3050, "change_pct": 30.0,
    }]


def test_fetch_upper_limits_distinguishes_empty_from_failure(monkeypatch):
    monkeypatch.setattr(kw, "_post", lambda *args, **kwargs: response({
        "return_code": 0, "updown_pric": [],
    }))
    assert kw.fetch_upper_limits("token") == []
    monkeypatch.setattr(kw, "_post", lambda *args, **kwargs: response({}, status=500))
    assert kw.fetch_upper_limits("token") is None
