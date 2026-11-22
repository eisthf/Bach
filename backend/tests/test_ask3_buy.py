from app.providers import kiwoom_api as kw
from app.providers.kiwoom import KiwoomBroker


class FakeData:
    _mock = False

    def __init__(self, ask3):
        self.ask3 = ask3
        self.calls = []

    def _call(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        return self.ask3 if fn is kw.fetch_ask3 else "12345"


def test_ask3_limit_buy_uses_quote_for_quantity_and_order_price():
    data = FakeData(12300)
    result = KiwoomBroker(data).buy_ask3("005930", 50000)
    assert result.ok and result.filled_qty == 4 and result.price == 12300
    fn, args, kwargs = data.calls[1]
    assert fn is kw.place_order
    assert args == ("005930", 4, "buy")
    assert kwargs["order_type"] == "0" and kwargs["price"] == "12300"
    assert kwargs["retry_auth"] is False


def test_missing_ask3_does_not_send_market_order():
    data = FakeData(0)
    result = KiwoomBroker(data).buy_ask3("005930", 50000)
    assert not result.ok
    assert len(data.calls) == 1


def test_ioc_order_types_and_cancel():
    """IOC 는 trde_tp 10, 단일가 1차(상한가 지정가)는 일반 지정가 0, 잔량 취소는 kt10003."""
    data = FakeData(12300)
    broker = KiwoomBroker(data)
    broker.buy_ask3("005930", 50000, True)
    assert data.calls[-1][2]["order_type"] == "10"
    broker.buy_limit("005930", 3, 16000, True)
    assert data.calls[-1][2]["order_type"] == "10"
    broker.buy_limit("005930", 3, 16000)
    assert data.calls[-1][2]["order_type"] == "0"
    res = broker.cancel_order("005930", "0072612")
    fn, args, kwargs = data.calls[-1]
    assert fn is kw.cancel_order and args == ("005930", "0072612") and kwargs["retry_auth"] is False
    assert res.ok


def test_cancel_order_request_body(monkeypatch):
    sent = {}

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"return_code": 0, "ord_no": "0072700"}

    def post(url, headers, body, timeout, retries=5):
        sent.update(url=url, api=headers["api-id"], body=body, retries=retries)
        return Resp()

    monkeypatch.setattr(kw, "_post", post)
    assert kw.cancel_order("tok", "005930", "0072612") == "0072700"
    assert sent["api"] == "kt10003" and sent["url"].endswith("/api/dostk/ordr") and sent["retries"] == 1
    assert sent["body"] == {"dmst_stex_tp": "KRX", "orig_ord_no": "0072612", "stk_cd": "005930", "cncl_qty": "0"}
