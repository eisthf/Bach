"""매도 주문 실패 시 재시도 검증.

매수 경로는 실패를 확인하는데(fill>0) 매도 경로는 확인하지 않아, 주문 전송이
실패해도 "팔았다"고 믿고 DONE 으로 가는 결함이 있었다 — 손절 실패 시 하락 중인
포지션이 방치된다. 수정 후에는 상태를 바꾸지 않고 다음 틱에 자연 재시도한다.
(fill=0 은 주문번호를 못 받은 전송 실패라 재시도해도 중복 주문이 아니다.)
"""
from app.models import Tick
from app.strategy.ulc import Phase

from conftest import make_engine


def _tick(price: float) -> Tick:
    return Tick(code="005930", price=price, high=price, low=price, open=10_000.0)


class FlakySell:
    """처음 n 회는 실패(0.0), 이후 성공하는 sell_fn."""

    def __init__(self, fail_times: int, price: float = 10_000.0):
        self.fails_left = fail_times
        self.price = price
        self.sold: list[int] = []

    async def __call__(self, qty: int) -> float:
        if self.fails_left > 0:
            self.fails_left -= 1
            return 0.0
        self.sold.append(qty)
        return self.price


async def buy_never(_a):
    raise AssertionError("매수가 나가면 안 됨")


def _holding_engine(logs=None):
    eng = make_engine(logs=logs)
    eng.legs = []          # all_filled=True → 손절 활성
    eng.phase = Phase.HOLDING
    eng.shares, eng.avg_cost = 10, 10_000.0
    eng._avg_synced = True
    return eng


async def test_exit_failure_keeps_state_then_retries():
    """손절 주문 실패 → 상태 유지, 다음 틱에 재시도 성공 → DONE."""
    logs: list = []
    sell = FlakySell(fail_times=1, price=9_400.0)
    eng = _holding_engine(logs)

    await eng.on_tick(_tick(9_400.0), buy_never, sell)  # sl=5% → 손절선 9,500
    assert eng.phase == Phase.HOLDING, "실패했는데 DONE 으로 가면 포지션 방치"
    assert eng.shares == 10, "실패했는데 팔았다고 믿으면 안 됨"
    assert any("⚠️" in m and "재시도" in m for m in logs)

    await eng.on_tick(_tick(9_400.0), buy_never, sell)  # 재시도 → 성공
    assert eng.phase == Phase.DONE
    assert eng.shares == 0
    assert sell.sold == [10]


async def test_trailing_exit_failure_keeps_trailing():
    """트레일링 스탑 주문 실패 → TRAILING 유지, 다음 틱 재시도."""
    sell = FlakySell(fail_times=1, price=10_700.0)
    eng = make_engine()
    eng.legs = []
    eng.phase = Phase.TRAILING
    eng.shares, eng.avg_cost = 10, 10_000.0
    eng._avg_synced = True
    eng.trail_max = 11_000.0

    price = 11_000.0 * 0.979  # trail_max 대비 -2.1% → 트레일링 스탑
    await eng.on_tick(_tick(price), buy_never, sell)
    assert eng.phase == Phase.TRAILING
    assert eng.shares == 10

    await eng.on_tick(_tick(price), buy_never, sell)
    assert eng.phase == Phase.DONE
    assert sell.sold == [10]


async def test_half_sell_failure_stays_pre_trailing():
    """반익절 주문 실패 → 수량·phase 유지(TRAILING 진입 안 함), 다음 틱 재시도."""
    logs: list = []
    sell = FlakySell(fail_times=1, price=10_600.0)
    eng = _holding_engine(logs)
    eng.config = eng.config.model_copy(update={"ulc_trailing": True})

    await eng.on_tick(_tick(10_600.0), buy_never, sell)  # tp=5% → 익절선 10,500
    assert eng.phase == Phase.HOLDING, "실패했는데 TRAILING 으로 가면 절반이 계좌에 남는다"
    assert eng.shares == 10
    assert any("반익절 주문 실패" in m for m in logs)

    await eng.on_tick(_tick(10_600.0), buy_never, sell)
    assert eng.phase == Phase.TRAILING
    assert eng.shares == 5
    assert sell.sold == [5]


async def test_single_share_no_half_sell_still_enters_trailing():
    """shares==1 이면 half=0 — 매도 없이 TRAILING 진입(기존 동작 유지)."""
    async def sell_never(_q):
        raise AssertionError("half=0 이면 매도가 나가면 안 됨")

    eng = _holding_engine()
    eng.config = eng.config.model_copy(update={"ulc_trailing": True})
    eng.shares = 1
    await eng.on_tick(_tick(10_600.0), buy_never, sell_never)
    assert eng.phase == Phase.TRAILING
    assert eng.shares == 1
