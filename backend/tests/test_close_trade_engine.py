"""종가 매매 엔진: 진입 시점, 차수 조건(직전 차수 체결가), 조기 익절, 분할 후 익절/손절, 영속화."""
import pytest

from app.models import CloseTradeConfig, Tick
from app.strategy.close_trade import CloseTradeEngine, CtPhase

D1, D2, D3 = "2026-09-22", "2026-09-23", "2026-09-24"


class Broker:
    """주문 콜백 가짜. 주문 시점 가격(현재 틱)으로 체결됐다고 응답한다."""

    def __init__(self):
        self.price = 0.0
        self.buys: list[int] = []
        self.sells: list[int] = []
        self.fail_buy = False

    async def buy(self, amount):
        if self.fail_buy:
            return 0.0
        self.buys.append(amount)
        return self.price

    async def sell(self, qty):
        self.sells.append(qty)
        return self.price


def make(**cfg):
    logs = []
    eng = CloseTradeEngine("000001", CloseTradeConfig(total_krw=1_000_000, **cfg), log=logs.append)
    return eng, Broker(), logs


async def tick(eng, br, price, day):
    br.price = price
    await eng.on_tick(Tick(code="000001", price=price, high=price, low=price, open=price),
                      day, br.buy, br.sell)


async def enter_now(eng, br, price=10_000, day=D1):
    br.price = price
    assert await eng.enter(price, day, True, br.buy)


def test_legs_follow_ratios():
    eng, _, _ = make(ratios=[20, 30, 50])
    assert [leg.amount for leg in eng.legs] == [200_000, 300_000, 500_000]


async def test_intraday_entry_waits_until_next_session():
    eng, br, _ = make()
    await enter_now(eng, br)
    assert eng.phase == CtPhase.WAIT_NEXT_DAY and eng.shares == 20 and br.buys == [200_000]
    # 같은 날엔 -10%도, +10%도 무시한다.
    await tick(eng, br, 9_000, D1)
    await tick(eng, br, 11_000, D1)
    assert br.buys == [200_000] and br.sells == []
    # 다음 거래일 첫 틱부터 감시(2차 조건: 1차 체결가 10,000 × 0.95 = 9,500).
    await tick(eng, br, 9_600, D2)
    assert eng.phase == CtPhase.ACCUMULATING and br.buys == [200_000]
    await tick(eng, br, 9_500, D2)
    assert br.buys == [200_000, 300_000]


async def test_closed_market_entry_buys_at_next_open_and_watches_immediately():
    eng, br, _ = make()
    br.price = 10_000
    assert await eng.enter(0, D1, False, br.buy)
    assert eng.phase == CtPhase.PENDING_OPEN and br.buys == []
    await tick(eng, br, 10_000, D2)           # 다음 장 첫 틱: 1차 시장가
    assert br.buys == [200_000] and eng.phase == CtPhase.ACCUMULATING
    await tick(eng, br, 9_500, D2)            # 같은 날 곧바로 2차 감시
    assert br.buys == [200_000, 300_000]


async def test_third_leg_uses_second_leg_fill_price():
    eng, br, _ = make()
    await enter_now(eng, br, 10_000)
    await tick(eng, br, 9_000, D2)            # 갭하락: 2차 @ 9,000 (한 틱에 한 차수)
    assert br.buys == [200_000, 300_000]
    assert eng.trigger_price(2) == pytest.approx(9_000 * 0.95)
    await tick(eng, br, 8_600, D2)            # 8,550 초과 → 대기
    assert len(br.buys) == 2
    await tick(eng, br, 8_550, D2)
    assert len(br.buys) == 3 and eng.phase == CtPhase.HOLDING


async def test_fill_events_replace_estimated_price_for_next_trigger():
    eng, br, _ = make()
    await enter_now(eng, br, 10_000)
    eng.on_fill("buy", 15, 10_100)            # 실체결: 부분체결 두 번
    eng.on_fill("buy", 5, 10_300)
    assert eng.legs[0].price == pytest.approx(10_150)
    assert eng.shares == 20
    assert eng.trigger_price(1) == pytest.approx(10_150 * 0.95)


async def test_early_take_profit_cancels_remaining_legs():
    eng, br, _ = make()
    await enter_now(eng, br, 10_000)
    await tick(eng, br, 10_499, D2)
    assert br.sells == []
    await tick(eng, br, 10_500, D2)           # 평단 +5%
    assert br.sells == [20] and eng.phase == CtPhase.DONE
    assert "조기 익절" in eng.exit_reason
    await tick(eng, br, 9_000, D2)            # 종료 후엔 아무것도 하지 않는다
    assert br.buys == [200_000]


async def test_early_take_profit_uses_average_after_second_leg():
    eng, br, _ = make(early_tp_pct=5)
    await enter_now(eng, br, 10_000)          # 20주
    await tick(eng, br, 9_500, D2)            # 2차 31주 → 평단 ≈ 9,696
    avg = eng.avg_cost
    assert avg == pytest.approx((10_000 * 20 + 9_500 * 31) / 51)
    await tick(eng, br, round(avg * 1.05) + 1, D2)
    assert br.sells == [51] and eng.phase == CtPhase.DONE


async def test_no_stop_loss_before_all_legs_filled():
    eng, br, _ = make(ratios=[20, 30, 50], add_drop_pcts=[20, 20])
    await enter_now(eng, br, 10_000)
    await tick(eng, br, 9_000, D2)            # -10%: 2차 조건(-20%) 미달, 손절도 없음
    assert br.sells == [] and eng.phase == CtPhase.ACCUMULATING


async def test_after_all_legs_take_profit_and_stop_loss():
    eng, br, _ = make(ratios=[50, 50], add_drop_pcts=[5], tp_pct=5, sl_pct=5)
    await enter_now(eng, br, 10_000)          # 50주
    await tick(eng, br, 9_500, D2)            # 2차 52주 → HOLDING
    assert eng.phase == CtPhase.HOLDING
    avg = eng.avg_cost
    await tick(eng, br, round(avg * 0.95) + 1, D2)
    assert br.sells == []
    await tick(eng, br, avg * 0.95, D2)
    assert br.sells == [eng.shares] and "손절" in eng.exit_reason


async def test_single_leg_goes_straight_to_holding():
    eng, br, _ = make(ratios=[100], add_drop_pcts=[])
    await enter_now(eng, br, 10_000)
    await tick(eng, br, 10_000, D2)
    assert eng.phase == CtPhase.HOLDING
    await tick(eng, br, 10_500, D2)
    assert br.sells == [100] and "익절" in eng.exit_reason


async def test_failed_first_buy_reverts_to_draft():
    eng, br, _ = make()
    br.fail_buy = True
    assert not await eng.enter(10_000, D1, True, br.buy)
    assert eng.phase == CtPhase.DRAFT and eng.shares == 0
    assert all(leg.status == "waiting" for leg in eng.legs)


async def test_failed_additional_leg_stops_accumulation():
    eng, br, _ = make()
    await enter_now(eng, br, 10_000)
    br.fail_buy = True
    await tick(eng, br, 9_500, D2)
    assert eng.phase == CtPhase.HOLDING
    assert [leg.status for leg in eng.legs] == ["filled", "failed", "failed"]


async def test_hand_off_stops_engine():
    eng, br, _ = make()
    await enter_now(eng, br, 10_000)
    eng.hand_off("사용자 수동 전환")
    await tick(eng, br, 5_000, D2)
    assert eng.phase == CtPhase.MANUAL and br.buys == [200_000] and br.sells == []


async def test_persistence_round_trip_resumes_same_state():
    eng, br, _ = make()
    await enter_now(eng, br, 10_000)
    eng.on_fill("buy", 20, 10_050)
    saved = eng.to_dict()
    back = CloseTradeEngine.from_dict("000001", eng.config, saved)
    assert back.phase == CtPhase.WAIT_NEXT_DAY and back.quiet_date == D1
    assert back.shares == 20 and back.avg_cost == pytest.approx(10_050)
    await tick(back, br, 10_050 * 0.95, D2)
    assert br.buys == [200_000, 300_000]


async def test_reconfigure_before_entry_rebuilds_plan_and_after_entry_keeps_legs():
    eng, br, _ = make()
    eng.reconfigure(CloseTradeConfig(total_krw=2_000_000, ratios=[50, 50], add_drop_pcts=[3]))
    assert [leg.amount for leg in eng.legs] == [1_000_000, 1_000_000]
    await enter_now(eng, br, 10_000)
    eng.reconfigure(CloseTradeConfig(total_krw=3_000_000, ratios=[50, 50], add_drop_pcts=[4],
                                     tp_pct=7))
    assert [leg.amount for leg in eng.legs] == [1_000_000, 1_500_000]
    assert eng.config.tp_pct == 7
    with pytest.raises(ValueError):
        eng.reconfigure(CloseTradeConfig(ratios=[20, 30, 50]))
