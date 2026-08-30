"""주문 중 이벤트 루프가 막히지 않는지 검증 (e8aed45 회귀 방지).

buy_fn 이 0.5초 블로킹(실주문의 requests.post + rate-limit 대기를 모사)하는
동안 heartbeat 태스크가 계속 돌아야 한다. 예전 동기 구현에서는 heartbeat 가
그 구간 동안 0회였다(수정 전 동작을 재현해 확인한 값).
"""
import asyncio
import time

from app.models import Tick
from app.strategy.ulc import Phase

from conftest import make_engine

ORDER_BLOCK_SEC = 0.5


def _blocking_order(_amount) -> float:
    time.sleep(ORDER_BLOCK_SEC)
    return 10_000.0


async def test_order_does_not_block_loop():
    eng = make_engine()
    eng.phase = Phase.INIT
    eng.setup()
    assert eng.phase == Phase.ACCUMULATING

    async def buy_fn(amount):
        return await asyncio.to_thread(_blocking_order, amount)

    beats = 0

    async def heartbeat():
        nonlocal beats
        while True:
            beats += 1
            await asyncio.sleep(0.01)

    hb = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)  # 워밍업
    base = beats

    t0 = time.monotonic()
    tick = Tick(code="005930", price=10_000.0, high=10_000.0, low=10_000.0,
                open=10_000.0)
    await eng.on_tick(tick, buy_fn, buy_fn)
    elapsed = time.monotonic() - t0

    during = beats - base
    hb.cancel()

    assert elapsed >= ORDER_BLOCK_SEC, "주문이 실제로 수행되지 않음"
    assert during >= 20, f"이벤트 루프가 막혔다 (heartbeat {during}회, 기대 ~50)"
    assert eng.shares > 0, "매수가 반영되지 않음"
