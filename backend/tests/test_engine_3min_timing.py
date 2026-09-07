"""3분봉 종가 타이밍의 경계·판단 가격 회귀 테스트."""
from app.models import Tick
from app.strategy.ulc import BuyLeg, Phase

from conftest import make_engine


def _tick(price: float, time: int) -> Tick:
    return Tick(
        code="005930", price=price, high=price, low=price,
        open=10_000.0, time=time,
    )


class RecordingBuy:
    def __init__(self, fill_price: float = 10_000.0):
        self.fill_price = fill_price
        self.amounts: list[int] = []

    async def __call__(self, amount: int) -> float:
        self.amounts.append(amount)
        return self.fill_price


async def sell_never(_qty):
    raise AssertionError("매도가 나가면 안 됨")


def _enabled_engine():
    eng = make_engine()
    eng.config = eng.config.model_copy(update={"use_3min_bar_timing": True})
    eng.phase = Phase.INIT
    eng.setup()
    return eng


async def test_waits_for_completed_bar_before_first_entry():
    """같은 3분 구간에서는 매수하지 않고, 다음 구간 첫 틱에 한 번만 진입."""
    eng = _enabled_engine()
    buy = RecordingBuy()

    await eng.on_tick(_tick(10_000.0, 9 * 3600), buy, sell_never)
    await eng.on_tick(_tick(10_100.0, 9 * 3600 + 179), buy, sell_never)
    assert buy.amounts == []

    await eng.on_tick(_tick(10_200.0, 9 * 3600 + 180), buy, sell_never)
    assert buy.amounts == [250_000]

    await eng.on_tick(_tick(10_300.0, 9 * 3600 + 181), buy, sell_never)
    assert buy.amounts == [250_000], "한 봉에서 전략을 두 번 평가하면 안 됨"


async def test_uses_previous_bar_close_not_new_bar_first_tick():
    """새 봉 첫 틱이 목표가를 찍어도 직전 봉 종가가 아니면 2차 매수하지 않음."""
    eng = _enabled_engine()
    eng.legs = [
        BuyLeg(target=10_000.0, amount=250_000, filled=True),
        BuyLeg(target=9_500.0, amount=250_000, filled=False),
    ]
    eng.shares, eng.avg_cost = 25, 10_000.0
    eng._avg_synced = True
    buy = RecordingBuy(fill_price=9_400.0)

    await eng.on_tick(_tick(9_600.0, 9 * 3600), buy, sell_never)
    await eng.on_tick(_tick(9_400.0, 9 * 3600 + 180), buy, sell_never)
    assert buy.amounts == [], "새 봉 첫 틱이 아니라 직전 종가로 판단해야 함"

    # 두 번째 봉은 장중 목표가 아래였다가 9,450에 마감한다.
    await eng.on_tick(_tick(9_450.0, 9 * 3600 + 359), buy, sell_never)
    await eng.on_tick(_tick(9_700.0, 9 * 3600 + 360), buy, sell_never)
    assert buy.amounts == [250_000]
    assert eng.phase == Phase.HOLDING


async def test_tick_mode_remains_immediate():
    """기본값 false의 기존 틱 기준 동작은 유지."""
    eng = make_engine()
    eng.phase = Phase.INIT
    eng.setup()
    buy = RecordingBuy()

    await eng.on_tick(_tick(10_000.0, 9 * 3600), buy, sell_never)
    assert buy.amounts == [250_000]


async def test_ignores_tick_without_timestamp_in_bar_mode():
    """경계를 알 수 없는 time=0 틱으로 잘못 주문하지 않음."""
    eng = _enabled_engine()
    buy = RecordingBuy()

    await eng.on_tick(_tick(10_000.0, 0), buy, sell_never)
    assert buy.amounts == []
