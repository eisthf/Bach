"""전일 종가와 이벤트 시가가 모두 준비될 때만 엔진을 생성하는지 검증."""
import asyncio
import calendar
import pytest

from app.market_clock import now_kst
from app.models import Bar, Tick, TradeState

MIDNIGHT = calendar.timegm(now_kst().date().timetuple())  # 봉 축의 당일 00:00


def bar(t: int, o: float, c: float) -> Bar:
    return Bar(time=t, open=o, high=max(o, c), low=min(o, c), close=c, volume=1)


class FakeData:
    """prev_close/day_open 실패를 흉내 내는 데이터 제공자."""

    def __init__(self, bars, x=None, z=None, raise_on_bars=False):
        self._bars = bars
        self._x, self._z = x, z
        self._raise = raise_on_bars
        self.bars_calls = 0

    def prev_close(self, code):
        return self._x

    def day_open(self, code):
        return self._z

    def get_bars(self, code, interval, lookback_extra=60):
        self.bars_calls += 1
        if self._raise:
            raise RuntimeError("ka10080 실패")
        return self._bars

    def last_tick(self, code):
        return None

    def stock_name(self, code):
        return None

    async def stream_ticks(self, code):
        while True:
            await asyncio.sleep(3600)
            yield Tick(code=code, price=0, high=0, low=0, open=0)


def _arm(hub, clock, code: str):
    stock = hub.add_stock(code)
    if stock.task:
        stock.task.cancel()
    stock.machine.state = TradeState.MONITOR
    clock.open()
    return stock


async def test_normal_path_skips_bars_fetch(mock_hub):
    """X/Z 를 전용 API 로 얻으면 분봉 조회를 아예 하지 않는다(09:00 부하 절감)."""
    hub, mgr, clock = mock_hub
    fake = FakeData(bars=[], x=10_000.0, z=10_500.0)
    hub.data = fake
    stock = _arm(hub, clock, "005930")
    await hub.apply_market_open()
    await asyncio.gather(*(s.setup_task for s in hub.stocks.values() if s.setup_task))
    assert stock.engine is not None
    assert (stock.engine.x, stock.engine.z) == (10_000.0, 10_500.0)
    assert fake.bars_calls == 0


async def test_bars_cannot_bypass_missing_verified_open(mock_hub):
    hub, mgr, clock = mock_hub
    hub.data = FakeData(bars=[bar(MIDNIGHT + 9 * 3600, 10400, 10450)], x=10000)
    stock = _arm(hub, clock, "005930")
    await hub.apply_market_open()
    await stock.setup_task
    assert stock.engine is None
    assert stock.machine.state == TradeState.MANUAL_TRADING
    assert hub.data.bars_calls == 0


async def test_unresolvable_gives_up_to_manual(mock_hub):
    """X/Z 를 끝내 못 구하면 추측하지 않고 그 종목만 수동매매로 인계."""
    hub, mgr, clock = mock_hub
    hub.data = FakeData(bars=[])  # 전용 API 도 None, 분봉도 빔
    stock = _arm(hub, clock, "005930")
    await hub.apply_market_open()
    await asyncio.gather(*(s.setup_task for s in hub.stocks.values() if s.setup_task))
    assert stock.engine is None
    assert stock.machine.state == TradeState.MANUAL_TRADING
    assert any("⚠️" in m and "셋업 실패" in m for m in mgr.logs())


async def test_midday_bar_is_not_accepted_as_session_open(mock_hub):
    hub, mgr, clock = mock_hub
    hub.data = FakeData(bars=[bar(MIDNIGHT + 12 * 3600, 11000, 11100)], x=10000)
    stock = _arm(hub, clock, "005930")
    await hub.apply_market_open()
    await asyncio.gather(*(s.setup_task for s in hub.stocks.values() if s.setup_task))
    assert stock.engine is None
    assert stock.machine.state == TradeState.MANUAL_TRADING


async def test_one_failure_does_not_block_others(mock_hub):
    """한 종목의 get_bars 예외가 다른 종목의 AUTO 진입을 막지 않는다."""
    hub, mgr, clock = mock_hub
    stock_bad = _arm(hub, clock, "111111")
    stock_ok = _arm(hub, clock, "222222")

    bad = FakeData(bars=[], raise_on_bars=True)
    ok = FakeData(bars=[], x=10_000.0, z=10_500.0)

    class Router:
        def __getattr__(self, name):
            def route(code, *a, **kw):
                return getattr(bad if code == "111111" else ok, name)(code, *a, **kw)
            return route

    hub.data = Router()
    await hub.apply_market_open()
    await asyncio.gather(*(s.setup_task for s in hub.stocks.values() if s.setup_task))

    assert stock_bad.engine is None
    assert stock_bad.machine.state == TradeState.MANUAL_TRADING
    assert stock_ok.engine is not None, "실패가 격리되지 않아 다음 종목까지 삼킴"
    assert stock_ok.machine.state == TradeState.AUTO_TRADING


async def test_delayed_open_starts_after_retry_without_orders_while_waiting(mock_hub):
    hub, mgr, clock = mock_hub
    hub.data = FakeData(bars=[], x=30550)
    stock = _arm(hub, clock, "386380")
    await hub.apply_market_open()
    pending = stock.setup_task
    assert stock.engine is None
    assert "대기" in stock.recovery_notice
    await asyncio.sleep(0.03)
    assert stock.engine is None
    hub.data._z = 31700
    await pending
    assert stock.engine.z == 31700
    assert stock.engine.shares == 0
    assert stock.recovery_notice == ""


async def test_slow_stock_does_not_delay_ready_stock(mock_hub):
    hub, mgr, clock = mock_hub
    slow = _arm(hub, clock, "111111")
    ready = _arm(hub, clock, "222222")
    original = hub._start_auto
    release = asyncio.Event()

    async def delayed(stock):
        if stock is slow:
            await release.wait()
        return await original(stock)

    hub._start_auto = delayed
    hub.data = FakeData(bars=[], x=10000, z=10000)
    await hub.apply_market_open()
    await ready.setup_task
    assert ready.engine is not None
    assert slow.engine is None
    release.set()
    await slow.setup_task


@pytest.mark.parametrize("action", ["push", "remove", "close", "reset"])
async def test_pending_setup_is_cancelled_by_user_or_market(mock_hub, action):
    hub, mgr, clock = mock_hub
    stock = _arm(hub, clock, "386380")
    entered = asyncio.Event()

    async def delayed(_stock):
        entered.set()
        await asyncio.sleep(60)
        raise AssertionError("취소된 셋업이 재개되면 안 됨")

    hub._start_auto = delayed
    await hub.apply_market_open()
    pending = stock.setup_task
    await entered.wait()
    if action == "push":
        hub.push(stock.code)
    elif action == "remove":
        hub.remove_stock(stock.code)
    elif action == "close":
        hub.apply_market_close()
    else:
        hub.apply_market_reset()
    await asyncio.gather(pending, return_exceptions=True)
    assert pending.cancelled()
    assert stock.engine is None
    assert stock.setup_task is None


async def test_slow_query_counts_toward_deadline(mock_hub):
    hub, mgr, clock = mock_hub
    stock = _arm(hub, clock, "386380")
    hub.AUTO_SETUP_TIMEOUT = 0.03

    async def stalled(_stock):
        await asyncio.sleep(60)

    hub._start_auto = stalled
    await hub.apply_market_open()
    await asyncio.wait_for(stock.setup_task, 1)
    assert stock.machine.state == TradeState.MANUAL_TRADING
    assert "시간 초과" in stock.recovery_notice
    assert stock.engine is None
