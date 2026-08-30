"""엔진 ↔ 계좌 보정(_sync_from_account) 규칙 검증 (5191d4d 회귀 방지).

주문 REST 응답에는 체결가가 없어(주문번호만) 엔진의 수량·평단은 추정치로
시작한다. 계좌(kt00018)를 진실원으로 삼되 보수적으로 보정하는 규칙들.
"""
from app.models import Tick
from app.strategy.ulc import Phase

from conftest import make_engine


async def test_partial_fill_shrinks_to_account():
    """계좌 잔량이 추정보다 적으면 실보유에 맞춘다(과다 매도 방지)."""
    eng = make_engine(account=(7, 10_500.0))
    eng.shares, eng.avg_cost = 10, 10_000.0
    await eng._sync_from_account()
    assert eng.shares == 7
    # 수량이 일치된 뒤에는 계좌 평단 채택
    assert eng.avg_cost == 10_500.0


async def test_preexisting_holdings_untouched():
    """계좌가 더 많으면 건드리지 않는다 — 자동매매 이전 보유분 보호."""
    eng = make_engine(account=(100, 9_000.0))
    eng.shares, eng.avg_cost = 10, 10_000.0
    await eng._sync_from_account()
    assert eng.shares == 10, "외부 물량까지 엔진이 떠안으면 안 됨"
    assert eng.avg_cost == 10_000.0, "외부 물량 섞인 평단을 채택하면 안 됨"


async def test_exact_match_adopts_account_avg():
    """수량이 정확히 일치할 때만 평단을 계좌 값으로 보정."""
    eng = make_engine(account=(10, 10_730.0))
    eng.shares, eng.avg_cost = 10, 10_000.0
    await eng._sync_from_account()
    assert eng.shares == 10
    assert eng.avg_cost == 10_730.0


async def test_account_zero_ignored():
    """계좌 0 보고는 무시 — 주문 직후 체결 미반영일 수 있다."""
    eng = make_engine(account=(0, 0.0))
    eng.shares, eng.avg_cost = 10, 10_000.0
    await eng._sync_from_account()
    assert eng.shares == 10


async def test_unavailable_account_keeps_estimate():
    """조회 불가(None/예외) 시 추정치 유지, 크래시 없음."""
    eng = make_engine(account=None)
    eng.shares, eng.avg_cost = 10, 10_000.0
    await eng._sync_from_account()
    assert (eng.shares, eng.avg_cost) == (10, 10_000.0)

    async def boom():
        raise RuntimeError("계좌 조회 실패")

    eng.position_fn = boom
    await eng._sync_from_account()
    assert (eng.shares, eng.avg_cost) == (10, 10_000.0)


async def test_exit_sells_actual_holdings():
    """청산은 추정(10주)이 아니라 실보유(7주)를 판다."""
    sold = []

    async def sell_fn(q):
        sold.append(q)
        return 11_000.0

    eng = make_engine(account=(7, 10_500.0))
    eng.shares, eng.avg_cost = 10, 10_000.0
    await eng._exit_all(sell_fn, "익절 전량")
    assert sold == [7], "과다 매도"
    assert eng.phase == Phase.DONE
    assert eng.shares == 0


async def test_correction_happens_before_tp_decision():
    """평단 보정이 익절 '판단 전에' 반영되는지 — 오익절 방지.

    추정 평단 10,000 → 실제 10,730. tp=5% 면 익절선이 10,500 → 11,266.5 로
    올라간다. 10,600 틱에서 낡은 평단이면 익절이 발동하지만(잘못), 보정이
    판단보다 먼저면 HOLDING 을 유지해야 한다. 주문 시점에만 보정하던 초기
    구현이 이 테스트로 걸러졌다.
    """
    sold = []

    async def sell_fn(q):
        sold.append(q)
        return 10_600.0

    logs: list = []
    eng = make_engine(account=(10, 10_730.0), logs=logs)
    eng.shares, eng.avg_cost = 10, 10_000.0
    eng.legs = []  # all_filled=True
    eng.phase = Phase.HOLDING
    price = 10_600.0
    await eng.on_tick(Tick(code="005930", price=price, high=price, low=price,
                           open=10_000.0), sell_fn, sell_fn)
    assert eng.phase == Phase.HOLDING and not sold, "낡은 평단으로 익절 발동"
    assert any("평단 보정" in m for m in logs)
