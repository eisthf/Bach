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
