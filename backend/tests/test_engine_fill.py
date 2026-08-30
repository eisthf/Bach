"""00(주문체결) 이벤트 → 엔진 평단 확정 검증 (95fd355 회귀 방지).

실체결(FID 914 단위체결가)은 계좌 폴링보다 빠르고 이 엔진의 주문만 반영하므로
평단의 1차 진실원이다. 계좌 평단은 실체결이 없을 때만 채택한다.
"""
from app.strategy.ulc import Phase

from conftest import make_engine


def test_buy_fills_accumulate_weighted_avg():
    eng = make_engine()
    eng.on_fill("buy", 10, 10_000.0)
    assert (eng.fill_qty, eng.avg_cost) == (10, 10_000.0)
    eng.on_fill("buy", 10, 11_000.0)
    assert (eng.fill_qty, eng.avg_cost) == (20, 10_500.0)


def test_partial_fills_accumulate():
    """부분체결이 여러 건으로 나뉘어 와도 누적된다."""
    eng = make_engine()
    for _ in range(4):
        eng.on_fill("buy", 5, 10_000.0)
    assert (eng.fill_qty, eng.avg_cost) == (20, 10_000.0)


def test_sell_reduces_qty_keeps_avg():
    """매도는 수량만 줄인다 — 주당 평단이 매도가에 오염되면 안 된다."""
    eng = make_engine()
    eng.on_fill("buy", 20, 10_000.0)
    eng.on_fill("sell", 8, 12_000.0)
    assert (eng.fill_qty, eng.avg_cost) == (12, 10_000.0)


async def test_fill_avg_beats_account_avg():
    """실체결 평단이 계좌 평단보다 우선 — 계좌 값은 기존 보유분이 섞인 집계."""
    eng = make_engine(account=(12, 9_000.0))
    eng.shares = 12
    eng.on_fill("buy", 12, 10_000.0)
    await eng._sync_from_account()
    assert eng.avg_cost == 10_000.0


async def test_account_avg_used_without_fills():
    """실체결이 없으면 계좌 평단 채택(기존 동작 유지)."""
    eng = make_engine(account=(12, 9_000.0))
    eng.shares, eng.avg_cost = 12, 8_000.0
    await eng._sync_from_account()
    assert eng.avg_cost == 9_000.0


def test_invalid_inputs_ignored():
    eng = make_engine()
    eng.on_fill("buy", 0, 10_000.0)   # 수량 0
    eng.on_fill("buy", 10, 0.0)       # 가격 0
    eng.on_fill("sell", 999, 0.0)     # 과매도 → 음수 금지
    assert (eng.fill_qty, eng.avg_cost) == (0, 0.0)
    eng.phase = Phase.SKIPPED
    eng.on_fill("buy", 10, 10_000.0)  # 진입 전/스킵 상태에선 무시
    assert eng.fill_qty == 0
