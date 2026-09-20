from datetime import datetime
import threading

from app.trade_history import TradeHistory
from app.providers.kiwoom import KiwoomDataProvider
from app.providers.mock import MockDataProvider, MockBroker
from app.market_clock import chart_epoch


def test_confirmed_fills_persist_and_deduplicate(tmp_path, monkeypatch):
    path = tmp_path / "state.trades.json"
    provider = KiwoomDataProvider.__new__(KiwoomDataProvider)
    provider.on_order_fill = lambda fill: None
    provider.trade_history = TradeHistory(path)
    monkeypatch.setattr("app.providers.kiwoom.now_kst", lambda: datetime(2026, 9, 21, 9, 5, 0))
    event = {"913": "접수", "9001": "A005930", "905": "+매수", "914": "10000", "915": "3", "9203": "order1", "908": "090259", "909": "fill1"}
    provider._handle_fill(event)
    assert provider.trade_history.for_code("005930") == []
    event["913"] = "체결"
    provider._handle_fill(event)
    provider._handle_fill(event)
    event["909"] = "fill2"
    provider._handle_fill(event)
    provider.trade_history.close()
    restored = TradeHistory(path)
    rows = restored.for_code("005930")
    restored.close()
    assert len(rows) == 2
    assert rows[0]["time"] == chart_epoch(datetime(2026, 9, 21, 9, 2, 59))
    assert rows[0]["time_source"] == "execution"
    other = TradeHistory(tmp_path / "other.json")
    assert other.for_code("005930") == []
    other.close()


def test_mock_filled_buy_and_sell_are_recorded(tmp_path, monkeypatch):
    data = MockDataProvider()
    data.trade_history = TradeHistory(tmp_path / "mock.trades.json")
    broker = MockBroker(data)
    monkeypatch.setattr(broker, "_price", lambda code: 1000)
    assert not broker.buy("005930", 1).ok
    assert broker.buy("005930", 5000).ok
    assert broker.sell("005930", 2).ok
    data.trade_history.close()
    rows = data.trade_history.for_code("005930")
    assert [(row["side"], row["qty"]) for row in rows] == [("buy", 5), ("sell", 2)]


def test_chart_storage_failure_does_not_change_order_result(monkeypatch):
    data = MockDataProvider()
    class BrokenHistory:
        def record(self, fill):
            raise RuntimeError("storage unavailable")
    data.trade_history = BrokenHistory()
    broker = MockBroker(data)
    monkeypatch.setattr(broker, "_price", lambda code: 1000)
    result = broker.buy("005930", 5000)
    assert result.ok and result.filled_qty == 5
    assert broker.position("005930").quantity == 5


def test_slow_storage_does_not_block_other_stock_order_or_chart(tmp_path, monkeypatch):
    data = MockDataProvider()
    history = data.trade_history = TradeHistory(tmp_path / "slow.json")
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    save = history._save

    def slow_save():
        entered.set()
        assert release.wait(5)
        save()

    monkeypatch.setattr(history, "_save", slow_save)
    broker = MockBroker(data)
    monkeypatch.setattr(broker, "_price", lambda code: 1000)
    results = []

    def other_order_and_read():
        results.append(broker.buy("000660", 3000))
        results.append(history.for_code("005930"))
        completed.set()

    thread = threading.Thread(target=other_order_and_read, daemon=True)
    try:
        assert broker.buy("005930", 5000).ok
        assert entered.wait(2)
        thread.start()
        # 저장 작업은 아직 막혀 있지만 다른 종목 주문과 차트 조회는 완료된다.
        assert completed.wait(2)
        assert results[0].ok and results[0].filled_qty == 3
        assert results[1][0]["qty"] == 5
    finally:
        release.set()
        if thread.ident:
            thread.join(2)
        history.close()
    restored = TradeHistory(tmp_path / "slow.json")
    try:
        assert restored.for_code("000660")[0]["qty"] == 3
    finally:
        restored.close()


def test_chart_lock_does_not_block_fill_submission(tmp_path):
    history = TradeHistory(tmp_path / "locked.json")
    completed = threading.Event()

    def submit():
        history.record(dict(code="005930", side="buy", qty=1, price=1000))
        completed.set()

    thread = threading.Thread(target=submit, daemon=True)
    try:
        with history.lock:
            thread.start()
            assert completed.wait(2)
    finally:
        thread.join(2)
        history.close()
    assert len(history.for_code("005930")) == 1


def test_writer_recovers_after_storage_failure(tmp_path, monkeypatch):
    history = TradeHistory(tmp_path / "retry.json")
    save = history._save
    attempts = []

    def fail_once():
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("disk temporarily unavailable")
        save()

    monkeypatch.setattr(history, "_save", fail_once)
    history.record(dict(code="005930", side="buy", qty=1, price=1000))
    history.record(dict(code="000660", side="sell", qty=2, price=2000))
    history.close()
    restored = TradeHistory(tmp_path / "retry.json")
    try:
        assert len(restored.for_code("005930")) == 1
        assert len(restored.for_code("000660")) == 1
        assert not history._worker.is_alive()
    finally:
        restored.close()
