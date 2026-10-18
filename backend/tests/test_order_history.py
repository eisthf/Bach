"""kt00007 계좌별 주문체결내역 — Bach 가 놓친 체결(서버가 꺼져 있을 때 HTS 주문)을 되짚는 조회."""
import pytest

from app.providers import kiwoom_api as kw


class _Resp:
    def __init__(self, payload, headers=None, status=200):
        self._payload, self.headers, self.status_code = payload, headers or {}, status

    def json(self):
        return self._payload


def _row(**kw_):
    base = {"ord_no": "0088041", "stk_cd": "A008970", "trde_tp": "시장가", "io_tp_nm": "현금매도",
            "cntr_qty": "0000000722", "cntr_uv": "0000001450", "ord_tm": "09:03:22",
            "comm_ord_tp": "영웅문S#", "dmst_stex_tp": "KRX", "mdfy_cncl": ""}
    return {**base, **kw_}


def test_parses_filled_orders_and_pages(monkeypatch):
    pages = [
        _Resp({"return_code": 0, "acnt_ord_cntr_prps_dtl": [
            _row(), _row(ord_no="0000001", cntr_qty="0000000000")]},     # 체결 0 → 제외
            {"cont-yn": "Y", "next-key": "k1"}),
        _Resp({"return_code": 0, "acnt_ord_cntr_prps_dtl": [
            _row(ord_no="0071755", io_tp_nm="현금매수", cntr_qty="0000000352", cntr_uv="0000001414",
                 ord_tm="09:00:10", comm_ord_tp="REST API")]}),
    ]
    sent = []

    def fake_post(url, headers, body, timeout=10):
        sent.append((headers.get("cont-yn"), body))
        return pages.pop(0)

    monkeypatch.setattr(kw, "_post", fake_post)
    monkeypatch.setattr("time.sleep", lambda s: None)
    rows = kw.fetch_order_history("tok", "20261006", "008970")
    assert sent[0][1]["ord_dt"] == "20261006" and sent[0][1]["qry_tp"] == "4" and sent[1][0] == "Y"
    assert rows == [
        {"code": "008970", "side": "sell", "qty": 722, "price": 1450.0, "order_no": "0088041", "time": "09:03:22",
         "order_type": "시장가", "channel": "영웅문S#", "exchange": "KRX", "modify": ""},
        {"code": "008970", "side": "buy", "qty": 352, "price": 1414.0, "order_no": "0071755", "time": "09:00:10",
         "order_type": "시장가", "channel": "REST API", "exchange": "KRX", "modify": ""},
    ]


def test_failure_is_an_error_not_an_empty_day(monkeypatch):
    monkeypatch.setattr(kw, "_post", lambda *a, **k: _Resp({"return_code": 1, "return_msg": "오류"}))
    with pytest.raises(kw.KiwoomRequestError):
        kw.fetch_order_history("tok", "20261006")
