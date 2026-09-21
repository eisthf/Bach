"""시가 틱 소비 후 새 틱이 없어도 진입하며 중복·오래된 틱은 주문하지 않는다."""
import asyncio
import threading
import time
from unittest.mock import Mock

import pytest

from app.market_clock import session_open_epoch
from app.models import OrderResult, Tick, TradeState
from app.strategy.ulc import Phase
from test_hub_start_auto import FakeData, _arm


def prepare(mock_hub, monkeypatch, **changes):
    hub, _, clock = mock_hub
    stamp = session_open_epoch() + 10
    monkeypatch.setattr('app.hub.chart_epoch', lambda: stamp)
    stock = _arm(hub, clock, '092780')
    hub.live = True
    stock.machine.state = TradeState.AUTO_TRADING
    stock.config.ulc_first_buy_ask3 = True
    tick = Tick(code=stock.code, price=4305, open=4305, high=4305,
                low=4305, open_verified=True, time=stamp,
                received_ns=time.perf_counter_ns()).model_copy(update=changes)
    data = FakeData([], x=4100, z=4305)
    data.last_tick = lambda _: tick
    hub.data = data
    hub.broker.buy_ask3 = Mock(return_value=OrderResult(
        ok=True, code=stock.code, side='buy', price=tick.price, filled_qty=58))
    hub.broker.buy = Mock(side_effect=AssertionError('중복/후속 매수'))
    return hub, stock, tick


async def test_consumed_open_tick_buys_without_another_tick(mock_hub, monkeypatch):
    hub, stock, tick = prepare(mock_hub, monkeypatch)

    async def stream(_):
        yield tick

    hub.data.stream_ticks = stream
    await hub._tick_loop(stock)  # 엔진이 없어 시가 틱은 표시만 되고 소비됨
    hub.broker.buy_ask3.assert_not_called()
    await hub._prepare_auto(stock)
    hub.broker.buy_ask3.assert_called_once()
    assert stock.engine.legs[0].filled


@pytest.mark.parametrize('changes', [
    {'open_verified': False}, {'time': 1}, {'received_ns': 0},
    {'received_ns': 1}, {'price': 0}, {'open': 0}, {'code': '001520'},
    {'received_ns': time.perf_counter_ns() + 60_000_000_000},
])
async def test_invalid_cached_tick_waits(mock_hub, monkeypatch, changes):
    hub, stock, _ = prepare(mock_hub, monkeypatch, **changes)
    await hub._prepare_auto(stock)
    hub.broker.buy_ask3.assert_not_called()


async def test_gap_guard_still_blocks_startup_buy(mock_hub, monkeypatch):
    hub, stock, _ = prepare(mock_hub, monkeypatch, price=5060)
    await hub._prepare_auto(stock)
    assert stock.engine.phase == Phase.SKIPPED
    hub.broker.buy_ask3.assert_not_called()


async def test_bar_mode_keeps_waiting_for_completed_bar(mock_hub, monkeypatch):
    hub, stock, _ = prepare(mock_hub, monkeypatch)
    stock.config.use_3min_bar_timing = True
    await hub._prepare_auto(stock)
    hub.broker.buy_ask3.assert_not_called()


async def test_live_tick_racing_startup_cannot_buy_twice(mock_hub, monkeypatch):
    hub, stock, tick = prepare(mock_hub, monkeypatch, price=4000)
    entered, release = threading.Event(), threading.Event()
    result = hub.broker.buy_ask3.return_value

    def buy(*args):
        entered.set()
        assert release.wait(3)
        return result

    hub.broker.buy_ask3.side_effect = buy
    startup = asyncio.create_task(hub._prepare_auto(stock))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        duplicate = asyncio.create_task(hub._run_engine_tick(stock, tick))
        await asyncio.sleep(0)
        assert not duplicate.done()
    finally:
        release.set()
    await startup
    await duplicate
    hub.broker.buy_ask3.assert_called_once()
    hub.broker.buy.assert_not_called()


async def test_real_tick_winning_race_prevents_startup_repeat(mock_hub, monkeypatch):
    hub, stock, tick = prepare(mock_hub, monkeypatch, price=4000)
    assert await hub._start_auto(stock)
    await hub._run_engine_tick(stock, tick)
    await hub._evaluate_startup_tick(stock)
    hub.broker.buy_ask3.assert_called_once()
    hub.broker.buy.assert_not_called()


async def test_handoff_before_startup_evaluation_blocks_buy(mock_hub, monkeypatch):
    hub, stock, _ = prepare(mock_hub, monkeypatch)
    assert await hub._start_auto(stock)
    hub.push(stock.code)
    await hub._evaluate_startup_tick(stock)
    hub.broker.buy_ask3.assert_not_called()


async def test_older_queue_is_ignored_but_new_ticks_continue(mock_hub, monkeypatch):
    hub, stock, tick = prepare(mock_hub, monkeypatch, price=4000)
    await hub._prepare_auto(stock)
    await hub._run_engine_tick(stock, tick.model_copy(update={'received_ns': tick.received_ns - 1}))
    hub.broker.buy.assert_not_called()
    hub.broker.buy.side_effect = None
    hub.broker.buy.return_value = hub.broker.buy_ask3.return_value
    await hub._run_engine_tick(stock, tick.model_copy(update={'received_ns': tick.received_ns + 1}))
    hub.broker.buy.assert_called_once()


async def test_default_market_buy_uses_same_startup_path(mock_hub, monkeypatch):
    hub, stock, _ = prepare(mock_hub, monkeypatch)
    stock.config.ulc_first_buy_ask3 = False
    hub.broker.buy.side_effect = None
    hub.broker.buy.return_value = hub.broker.buy_ask3.return_value
    await hub._prepare_auto(stock)
    hub.broker.buy.assert_called_once()
    hub.broker.buy_ask3.assert_not_called()


async def test_order_exception_is_not_retried_as_setup_failure(mock_hub, monkeypatch):
    hub, stock, _ = prepare(mock_hub, monkeypatch)
    hub.broker.buy_ask3.side_effect = RuntimeError('응답 불명')
    with pytest.raises(RuntimeError, match='응답 불명'):
        await hub._prepare_auto(stock)
    hub.broker.buy_ask3.assert_called_once()
