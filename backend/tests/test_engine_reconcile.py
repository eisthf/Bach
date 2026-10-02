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


async def test_late_fill_of_pending_leg_restores_shares():
    """미체결 분할 매수로 줄인 수량은 나중 체결(00)로 되돌린다.

    2026-10-01 008970: 3차 지정가가 미체결인 채 계좌 보정이 622→404주로 줄였고,
    3차 218주가 체결된 뒤에도 404주만 손절해 218주가 보호 없이 남았다.
    """
    account = {"pos": (404, 1_640.0)}

    async def position_fn():
        return account["pos"]

    eng = make_engine()
    eng.position_fn = position_fn
    eng.on_fill("buy", 197, 1_671.0)
    eng.on_fill("buy", 207, 1_611.0)
    eng.shares = 622                      # 3차 218주 주문 직후 추정
    await eng._sync_from_account()
    assert eng.shares == 404              # 3차 미체결 — 지금은 실보유에 맞춘다

    eng.on_fill("buy", 218, 1_520.0)      # 2분 뒤 3차 체결
    assert eng.shares == 622

    sold = []

    async def sell_fn(q):
        sold.append(q)
        return 1_480.0

    account["pos"] = (622, 1_598.0)
    await eng._exit_all(sell_fn, "손절")
    assert sold == [622], "3차 체결분까지 전량 매도해야 함"


async def test_account_lag_does_not_shrink_below_confirmed_fills():
    """계좌가 체결을 늦게 반영해도 실체결로 확인된 수량 밑으로 줄이지 않는다."""
    eng = make_engine(account=(197, 1_671.0))
    eng.on_fill("buy", 197, 1_671.0)
    eng.on_fill("buy", 207, 1_611.0)
    eng.shares = 404
    await eng._sync_from_account()
    assert eng.shares == 404
    assert eng._unconfirmed == 0


async def test_fill_counted_once_when_restoring():
    """줄인 수량보다 많은 체결이 와도 줄인 만큼만 되돌린다(이중 계산 방지)."""
    eng = make_engine(account=(10, 10_000.0))
    eng.on_fill("buy", 10, 10_000.0)
    eng.shares = 15
    await eng._sync_from_account()
    assert (eng.shares, eng._unconfirmed) == (10, 5)
    eng.on_fill("buy", 3, 9_900.0)
    eng.on_fill("buy", 4, 9_900.0)
    assert (eng.shares, eng._unconfirmed) == (15, 0)


async def test_no_restore_after_done():
    """청산 완료(DONE) 뒤 늦은 체결은 엔진 수량을 살리지 않는다(허브가 잔량 경고)."""
    eng = make_engine(account=(10, 10_000.0))
    eng.on_fill("buy", 10, 10_000.0)
    eng.shares = 15
    await eng._sync_from_account()
    eng.phase = Phase.DONE
    eng.shares = 0
    eng.on_fill("buy", 5, 9_900.0)
    assert eng.shares == 0
