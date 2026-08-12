"""분할매수 미완료(ACCUMULATING) 구간의 익절 활성 검증.

익절이 HOLDING 블록에만 있던 시절에는, 1차 매수 후 2차 목표가까지 내려오지
않고 바로 급등하면 — 이 전략의 최선 시나리오 — 익절이 영영 발동하지 않았다.
원 명세는 손절에만 '2차/3차 체결 이후' 게이트를 걸 뿐 익절에는 걸지 않는다.
"""
from app.models import Tick
from app.strategy.ulc import BuyLeg, Phase

from conftest import make_engine


def _tick(price: float) -> Tick:
    return Tick(code="005930", price=price, high=price, low=price, open=10_000.0)


def _accumulating_engine(avg: float = 10_000.0, leg2_target: float = 9_500.0):
    """1차만 체결된 SC1 모양의 엔진: 2차 leg 는 미체결로 남아 있다."""
    eng = make_engine()
    eng.legs = [BuyLeg(target=10_000.0, amount=250_000, filled=True),
                BuyLeg(target=leg2_target, amount=250_000, filled=False)]
    eng.shares, eng.avg_cost = 25, avg
    eng._avg_synced = True  # 평단 확정 상태로 두고 익절 로직만 본다
    return eng


async def test_tp_fires_during_accumulating():
    """1차 매수 후 급등: 2차 미체결이어도 익절 전량 매도 + 남은 leg 소멸."""
    sold = []

    async def sell_fn(q):
        sold.append(q)
        return 10_600.0

    async def buy_fn(a):
        raise AssertionError("익절 국면에서 매수가 나가면 안 됨")

    eng = _accumulating_engine()
    await eng.on_tick(_tick(10_600.0), buy_fn, sell_fn)  # tp=5% → 익절선 10,500
    assert sold == [25], "ACCUMULATING 에서 익절이 발동해야 함"
    assert eng.phase == Phase.DONE
    assert not eng.legs[1].filled, "남은 분할 leg 는 실행되지 않고 소멸"


async def test_tp_enters_trailing_during_accumulating():
    """trailing=ON: ACCUMULATING 중 익절 도달 → 절반 매도 후 TRAILING 진입."""
    sold = []

    async def sell_fn(q):
        sold.append(q)
        return 10_600.0

    async def buy_fn(a):
        return 0.0

    eng = _accumulating_engine()
    eng.config = eng.config.model_copy(update={"ulc_trailing": True})
    await eng.on_tick(_tick(10_600.0), buy_fn, sell_fn)
    assert sold == [12], "절반(25//2) 매도"
    assert eng.phase == Phase.TRAILING
    assert eng.trail_max == 10_600.0

    # 이후 트레일링 스탑(t=2%)까지 하락 → 잔량 청산
    await eng.on_tick(_tick(10_600.0 * 0.979), buy_fn, sell_fn)
    assert eng.phase == Phase.DONE
    assert sold == [12, 13]


async def test_sl_still_gated_during_accumulating():
    """손절 게이트는 유지: 2차 체결 전에는 sl 하락에도 매도하지 않는다."""
    sold = []

    async def sell_fn(q):
        sold.append(q)
        return 9_520.0

    async def buy_fn(a):
        return 0.0

    # 손절선(avg×0.95=9,500)보다 낮은 2차 목표가(9,400) → 9,520 틱은
    # 매수도 손절도 아니어야 한다.
    eng = _accumulating_engine(leg2_target=9_400.0)
    await eng.on_tick(_tick(9_520.0), buy_fn, sell_fn)
    assert not sold, "2차 체결 전 손절 발동은 명세 위반"
    assert eng.phase == Phase.ACCUMULATING
    assert eng.shares == 25
