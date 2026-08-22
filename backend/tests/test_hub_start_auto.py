"""장 시작 엔진 셋업(X/Z 확보) 검증.

- X/Z 폴백은 고정 인덱스가 아니라 **날짜 경계**로 역산한다. 예전의
  bars[-131]/[-130] 산술은 "당일 130봉이 꽉 차 있다"를 가정했는데, 이 코드가
  도는 09:00 에는 당일 봉이 0~1개라 항상 어긋났다(X 로 전일 12:30 가격을 집음).
- X/Z 를 못 구하면 추측하지 않고 그 종목만 수동매매로 인계한다.
- 한 종목의 셋업 실패·예외가 다른 종목의 장 시작 처리를 삼키지 않는다.
"""
import asyncio
import calendar

from app.market_clock import now_kst
from app.models import Bar, Tick, TradeState

MIDNIGHT = calendar.timegm(now_kst().date().timetuple())  # 봉 축의 당일 00:00


def bar(t: int, o: float, c: float) -> Bar:
    return Bar(time=t, open=o, high=max(o, c), low=min(o, c), close=c)


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
    assert stock.engine is not None
    assert (stock.engine.x, stock.engine.z) == (10_000.0, 10_500.0)
    assert fake.bars_calls == 0


async def test_fallback_uses_date_boundary(mock_hub):
    """09:00 상황(전일 꼬리 + 당일 1봉)에서 X=전일 마지막 close, Z=당일 첫 open.

    옛 인덱스 산술이라면 len=3 < 131 이라 bars[0](전일 첫 원소)의 close 를
    X 로 집었을 상황 — 날짜 경계 방식은 전일 '마지막' 봉을 정확히 고른다.
    """
    hub, mgr, clock = mock_hub
    bars = [
        bar(MIDNIGHT - 360, 9_700.0, 9_800.0),          # 전일 15:24봉
        bar(MIDNIGHT - 180, 9_800.0, 10_000.0),         # 전일 15:27봉 ← close 가 X
        bar(MIDNIGHT + 9 * 3600, 10_400.0, 10_450.0),   # 당일 09:00봉 ← open 이 Z
    ]
    hub.data = FakeData(bars=bars)
    stock = _arm(hub, clock, "005930")
    await hub.apply_market_open()
    assert stock.engine is not None
    assert stock.engine.x == 10_000.0, "전일 '마지막' 봉의 close 여야 함"
    assert stock.engine.z == 10_400.0, "당일 '첫' 봉의 open 이어야 함"


async def test_unresolvable_gives_up_to_manual(mock_hub):
    """X/Z 를 끝내 못 구하면 추측하지 않고 그 종목만 수동매매로 인계."""
    hub, mgr, clock = mock_hub
    hub.data = FakeData(bars=[])  # 전용 API 도 None, 분봉도 빔
    stock = _arm(hub, clock, "005930")
    await hub.apply_market_open()
    assert stock.engine is None
    assert stock.machine.state == TradeState.MANUAL_TRADING
    assert any("⚠️" in m and "셋업 실패" in m for m in mgr.logs())


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

    assert stock_bad.engine is None
    assert stock_bad.machine.state == TradeState.MANUAL_TRADING
    assert stock_ok.engine is not None, "실패가 격리되지 않아 다음 종목까지 삼킴"
    assert stock_ok.machine.state == TradeState.AUTO_TRADING
