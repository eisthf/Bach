import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import timing
from app.models import Tick, OrderResult, TradeState
from app.providers import kiwoom_api as kw
from app.strategy.ulc import UlcEngine


def test_gate_and_http_time_are_separate(monkeypatch):
    readings = iter([1000000, 4000000, 11000000])
    monkeypatch.setattr(timing, "perf_counter_ns", lambda: next(readings))
    monkeypatch.setattr(kw, "_reserve_slot", lambda **_: None)
    response = SimpleNamespace(status_code=200, json=lambda: {"return_code": 0})
    monkeypatch.setattr(kw.requests, "post", lambda *a, **k: response)
    trace = {}
    token = timing.order_trace.set(trace)
    try:
        kw._post("test", {"api-id": "kt10000"}, {}, 1, retries=1)
    finally:
        timing.order_trace.reset(token)
    assert timing.durations(trace) == {"rest_gate_wait_ms": 3.0, "http_response_ms": 7.0}


def test_network_failure_still_has_http_duration(monkeypatch):
    monkeypatch.setattr(kw, "_reserve_slot", lambda **_: None)
    monkeypatch.setattr(kw, "_penalize", lambda seconds: None)
    def fail(*args, **kwargs):
        raise kw.requests.Timeout("timeout")
    monkeypatch.setattr(kw.requests, "post", fail)
    trace = {}
    token = timing.order_trace.set(trace)
    try:
        with pytest.raises(kw.KiwoomRequestError):
            kw._post("test", {"api-id": "kt10000"}, {}, 1, retries=1)
    finally:
        timing.order_trace.reset(token)
    assert "http_response_ms" in timing.durations(trace)


async def test_parallel_orders_have_separate_traces_and_report_after_return(mock_hub, monkeypatch):
    hub, _, _ = mock_hub
    recorded = []
    returned = set()
    def record(text, **fields):
        if fields.get("event") == "auto_buy_timing":
            assert fields["code"] in returned
            recorded.append(fields)
    monkeypatch.setattr("app.hub.record_event", record)
    def buy(code, amount):
        timing.mark("worker_start")
        timing.mark("gate_start")
        timing.mark("http_start")
        timing.mark("http_end")
        returned.add(code)
        return OrderResult(ok=True, code=code, side="buy", filled_qty=25,
                           price=10000, order_no=f"order-{code}")
    hub.broker.buy = buy
    stocks = []
    for code in ["111111", "222222"]:
        stock = hub.add_stock(code)
        stock.task.cancel()
        stock.machine.state = TradeState.AUTO_TRADING
        stock.engine = UlcEngine(code=code, config=stock.config, x=10000, z=10000, log=hub._log)
        stock.engine.setup()
        stocks.append(stock)
    await asyncio.gather(*(hub._run_engine_tick(s, Tick(code=s.code, price=10000,
        open=10000, high=10000, low=10000, received_ns=timing.perf_counter_ns())) for s in stocks))
    assert len(recorded) == 2
    assert len({r["trace_id"] for r in recorded}) == 2
    for r in recorded:
        assert r["order_no"] == f"order-{r['code']}"
        assert r["http_attempted"]
        assert r["tick_to_http_ms"] >= 0
    assert timing.order_trace.get() is None


async def test_three_minute_timing_uses_evaluated_bar_tick():
    from app.models import AutoConfig
    engine = UlcEngine(code="111111", config=AutoConfig(use_3min_bar_timing=True),
                       x=10000, z=10000, log=lambda msg: None)
    engine.setup()
    received = []
    async def buy(amount):
        received.append(engine.decision_tick_received_ns)
        return 10000
    async def sell(qty):
        raise AssertionError("unexpected sell")
    await engine.on_tick(Tick(code="111111", price=10000, open=10000, high=10000,
                             low=10000, time=180, received_ns=123), buy, sell)
    await engine.on_tick(Tick(code="111111", price=10000, open=10000, high=10000,
                             low=10000, time=360, received_ns=456), buy, sell)
    assert received == [123]
