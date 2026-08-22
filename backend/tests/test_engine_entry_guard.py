"""지연 진입 가드 검증.

갭 필터(Z >= X*(1+w) → SKIP)는 셋업 시점의 Z 로만 평가되는데, 1차 매수는
가격 조건 없이 첫 틱에 발화한다. 셋업이 밀리거나 첫 틱이 늦으면 필터가
걸렀어야 할 가격에 그대로 진입하므로, 진입 전에는 실가격으로 다시 건다.
"""
from app.models import Tick
from app.strategy.ulc import BuyLeg, Phase

from conftest import make_engine

X = 10_000.0
LIMIT = X * 1.15  # 기본 w=0.15 → 11,500


def _tick(price: float) -> Tick:
    return Tick(code="005930", price=price, high=price, low=price, open=X)


class RecordingBuy:
    def __init__(self, price: float):
        self.price = price
        self.bought: list[int] = []

    async def __call__(self, amount: int) -> float:
        self.bought.append(amount)
        return self.price


async def sell_never(_q):
    raise AssertionError("매도가 나가면 안 됨")


def _fresh_engine():
    """셋업 직후(SC1, 아직 미체결) 상태의 엔진."""
    eng = make_engine(x=X, z=X)
    eng.phase = Phase.INIT
    eng.setup()
    assert eng.phase == Phase.ACCUMULATING
    return eng


async def test_skips_when_price_gapped_past_limit_before_entry():
    """진입 전 현재가가 갭 상한을 넘었으면 매수하지 않고 SKIP."""
    logs: list = []
    eng = make_engine(x=X, z=X, logs=logs)
    eng.phase = Phase.INIT
    eng.setup()
    buy = RecordingBuy(LIMIT + 100)

    await eng.on_tick(_tick(LIMIT + 100), buy, sell_never)
    assert eng.phase == Phase.SKIPPED
    assert not buy.bought, "필터가 걸렀어야 할 가격에 매수가 나감"
    assert eng.shares == 0
    assert any("진입 지연" in m for m in logs)


async def test_normal_entry_unaffected():
    """상한 이내면 기존대로 1차 매수가 즉시 발화."""
    eng = _fresh_engine()
    buy = RecordingBuy(X)
    await eng.on_tick(_tick(X), buy, sell_never)
    assert eng.phase == Phase.ACCUMULATING
    assert eng.shares > 0
    assert len(buy.bought) == 1


async def test_boundary_just_below_limit_enters():
    """경계 바로 아래(11,499)는 진입 — 조건이 >= 이므로."""
    eng = _fresh_engine()
    buy = RecordingBuy(LIMIT - 1)
    await eng.on_tick(_tick(LIMIT - 1), buy, sell_never)
    assert eng.phase == Phase.ACCUMULATING
    assert eng.shares > 0


async def test_guard_does_not_fire_after_entry():
    """이미 보유 중이면 급등해도 SKIP 하지 않는다(익절 경로로 가야 함)."""
    eng = make_engine(x=X, z=X)
    eng.legs = [BuyLeg(target=X, amount=250_000, filled=True),
                BuyLeg(target=9_500.0, amount=250_000, filled=False)]
    eng.shares, eng.avg_cost = 25, X
    eng._avg_synced = True

    sold = []

    async def sell_fn(q):
        sold.append(q)
        return LIMIT + 500

    async def buy_never(_a):
        raise AssertionError("매수가 나가면 안 됨")

    await eng.on_tick(_tick(LIMIT + 500), buy_never, sell_fn)
    assert eng.phase != Phase.SKIPPED, "보유 중인데 SKIP 하면 포지션이 방치된다"
    assert sold == [25], "급등이면 익절이 발동해야 함"
