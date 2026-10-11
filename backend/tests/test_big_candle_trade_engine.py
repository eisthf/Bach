"""대금 양봉 매매 엔진: 매입 구간, 1차 진입·기한, 2차 조건(다음 날·구간 안·1차가 −u%),
절반 익절 + 트레일링, 구간 이탈 손절, 분 단위 감시, 영속화."""
import pytest

from app.models import CandleTradeConfig, Tick
from app.strategy.big_candle_trade import CandleTradeEngine, CzPhase

# 기준 캔들: 시가 10,000 → 종가 12,000 (몸통 2,000). x=10% → 구간 10,200 ~ 11,800
BASE = "2026-09-21"
D1, D2, D3, D4 = "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"


class Broker:
    """주문 콜백 가짜. 주문 시점 가격(현재 틱)으로 체결됐다고 응답한다."""

    def __init__(self):
        self.price = 0.0
        self.buys: list[int] = []
        self.sells: list[int] = []
        self.fail_buy = False
        self.fail_sell = False

    async def buy(self, amount):
        if self.fail_buy:
            return 0.0
        self.buys.append(amount)
        return self.price

    async def sell(self, qty):
        if self.fail_sell:
            return 0
        self.sells.append(qty)
        return qty


def make(**cfg):
    logs = []
    eng = CandleTradeEngine("000001", CandleTradeConfig(total_krw=1_000_000, **cfg), log=logs.append)
    eng.set_base(BASE, 10_000, 12_000, confirmed=True)
    eng.start()
    return eng, Broker(), logs


def day(eng, d, elapsed, prev_close=11_000.0):
    eng.begin_day(d, prev_close, elapsed)


async def tick(eng, br, price, d, t=0):
    br.price = price
    await eng.on_tick(Tick(code="000001", price=price, high=price, low=price, open=price, time=t),
                      d, br.buy, br.sell)


def test_zone_from_body():
    eng, _, _ = make()
    assert eng.zone() == pytest.approx((10_200, 11_800))
    eng2, _, _ = make(zone_margin_pct=25)
    assert eng2.zone() == pytest.approx((10_500, 11_500))
    assert [leg.amount for leg in eng.legs] == [500_000, 500_000]


def test_start_requires_bullish_base():
    eng = CandleTradeEngine("000001", CandleTradeConfig())
    eng.set_base(BASE, 12_000, 12_000, confirmed=True)
    with pytest.raises(ValueError):
        eng.start()


async def test_no_evaluation_on_base_day():
    eng, br, _ = make()
    day(eng, BASE, 0)
    await tick(eng, br, 11_000, BASE)
    assert br.buys == [] and eng.phase == CzPhase.WATCHING


async def test_first_buy_when_price_enters_zone():
    eng, br, _ = make()
    day(eng, D1, 1)
    await tick(eng, br, 12_500, D1)         # 구간 위
    await tick(eng, br, 11_900, D1)
    assert br.buys == []
    await tick(eng, br, 11_800, D1)         # 상단 경계 포함
    assert br.buys == [500_000] and eng.phase == CzPhase.ACCUMULATING
    assert eng.shares == 42 and eng.first_date == D1


async def test_tick_ignored_before_begin_day():
    eng, br, _ = make()
    await tick(eng, br, 11_000, D1)         # 오늘 맥락이 없으면 평가하지 않는다
    assert br.buys == []


async def test_expires_after_entry_days():
    eng, br, logs = make(entry_days=5)
    day(eng, "2026-09-28", 5)
    assert eng.phase == CzPhase.WATCHING     # 5번째 거래일까지는 유효
    day(eng, "2026-09-29", 6)
    assert eng.phase == CzPhase.DONE and "기한 만료" in eng.exit_reason
    await tick(eng, br, 11_000, "2026-09-29")
    assert br.buys == []


async def test_second_buy_next_day_in_zone_and_below_first():
    eng, br, _ = make()
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)          # 1차 @ 11,000 → 2차 상한 10,450
    await tick(eng, br, 10_400, D1)          # 같은 날은 2차 안 함
    assert br.buys == [500_000]
    day(eng, D2, 2)
    await tick(eng, br, 10_500, D2)          # 1차 대비 −4.5%: 부족
    assert br.buys == [500_000]
    await tick(eng, br, 10_100, D2)          # −8%지만 구간 하단(10,200) 아래
    assert br.buys == [500_000]
    await tick(eng, br, 10_450, D2)          # 구간 안 & ≤ 10,450
    assert br.buys == [500_000, 500_000] and eng.phase == CzPhase.HOLDING
    assert eng.avg_cost == pytest.approx((45 * 11_000 + 47 * 10_450) / 92)


async def test_half_take_profit_cancels_second_leg_then_trails():
    eng, br, logs = make()
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)          # 45주 @ 11,000, 익절가 13,200
    await tick(eng, br, 13_199, D1)
    assert br.sells == []
    await tick(eng, br, 13_200, D1)          # 매수 당일이라도 익절 조건이면 익절
    assert br.sells == [22] and eng.phase == CzPhase.TRAILING
    assert eng.legs[1].status == "canceled" and eng.shares == 23
    await tick(eng, br, 14_000, D1)          # 고점 갱신 → 스톱 13,300
    await tick(eng, br, 13_400, D1)
    assert br.sells == [22]
    assert eng.trailing_stop() == pytest.approx(13_300)
    await tick(eng, br, 13_300, D1)
    assert br.sells == [22, 23] and eng.phase == CzPhase.DONE and "트레일링" in eng.exit_reason


async def test_take_profit_after_both_legs_uses_average():
    eng, br, _ = make()
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)
    day(eng, D2, 2)
    await tick(eng, br, 10_450, D2)
    target = eng.avg_cost * 1.2
    await tick(eng, br, target - 1, D3)      # day 맥락은 D2 — D3 틱은 무시
    day(eng, D3, 3)
    await tick(eng, br, target - 1, D3)
    assert br.sells == []
    await tick(eng, br, target + 1, D3)
    assert br.sells == [46] and eng.phase == CzPhase.TRAILING


async def test_single_share_takes_profit_fully():
    eng, br, _ = make()
    eng.reconfigure(CandleTradeConfig(total_krw=20_000))
    eng.set_base(BASE, 5_000, 6_000, confirmed=True)
    day(eng, D1, 1)
    await tick(eng, br, 5_500, D1)           # 10,000원 // 5,500 = 1주
    assert eng.shares == 1
    await tick(eng, br, 6_600, D1)
    assert br.sells == [1] and eng.phase == CzPhase.DONE


async def test_zone_break_stop_after_close_below_low():
    eng, br, logs = make()
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)
    day(eng, D2, 2, prev_close=10_300)       # 하단 위 마감 → 손절 꺼짐
    await tick(eng, br, 10_000, D2)
    assert br.sells == [] and not eng.stop_armed
    day(eng, D3, 3, prev_close=10_100)       # 하단 아래 마감 → 오늘 하단 아래 틱에서 전량
    assert eng.stop_armed
    await tick(eng, br, 10_200, D3)          # 하단 그 자체는 '아래'가 아니다(2차 조건엔 맞음)
    assert br.sells == [] and br.buys == [500_000, 500_000]
    await tick(eng, br, 10_199, D3)
    assert br.sells == [45 + 49] and eng.phase == CzPhase.DONE and "구간 이탈" in eng.exit_reason


async def test_zone_break_not_armed_on_first_buy_day():
    eng, br, _ = make()
    day(eng, D1, 1, prev_close=9_000)        # 1차 매수 당일의 전일 종가는 따지지 않는다
    await tick(eng, br, 11_000, D1)
    await tick(eng, br, 10_000, D1)
    assert br.sells == [] and eng.phase == CzPhase.ACCUMULATING


async def test_zone_break_stop_applies_while_trailing():
    eng, br, _ = make()
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)
    await tick(eng, br, 13_200, D1)          # 절반 익절
    day(eng, D2, 2, prev_close=10_000)
    await tick(eng, br, 10_100, D2)
    assert br.sells == [22, 23] and eng.phase == CzPhase.DONE


async def test_first_buy_failure_ends():
    eng, br, _ = make()
    br.fail_buy = True
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)
    assert eng.phase == CzPhase.DONE and eng.exit_reason == "1차 매수 실패"


async def test_second_buy_failure_stops_adding():
    eng, br, _ = make()
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)
    br.fail_buy = True
    day(eng, D2, 2)
    await tick(eng, br, 10_400, D2)
    assert eng.phase == CzPhase.HOLDING and eng.legs[1].status == "failed"
    br.fail_buy = False
    await tick(eng, br, 10_300, D2)
    assert br.buys == [500_000]


async def test_half_sell_failure_hands_off():
    eng, br, _ = make()
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)
    br.fail_sell = True
    await tick(eng, br, 13_500, D1)
    assert eng.phase == CzPhase.MANUAL


async def test_minute_mode_uses_completed_bar_close():
    eng, br, _ = make(watch_mode="minute", watch_minutes=3)
    day(eng, D1, 1)
    t0 = 1_800_000_000 // 180 * 180
    await tick(eng, br, 11_000, D1, t0)          # 봉 안에서 구간에 들어왔다가
    await tick(eng, br, 12_100, D1, t0 + 60)     # 같은 봉 종가는 구간 위
    await tick(eng, br, 12_200, D1, t0 + 180)    # 다음 봉 첫 틱 → 직전 봉(12,100) 확정
    assert br.buys == []
    await tick(eng, br, 11_500, D1, t0 + 200)
    await tick(eng, br, 12_300, D1, t0 + 360)    # 직전 봉 종가 11,500 → 1차
    assert br.buys == [500_000]


async def test_minute_mode_drops_previous_day_bar():
    eng, br, _ = make(watch_mode="minute", watch_minutes=3)
    day(eng, D1, 1)
    t0 = 1_800_000_000 // 180 * 180
    await tick(eng, br, 11_000, D1, t0)          # 장 마감 직전 봉(미확정)
    day(eng, D2, 2)
    await tick(eng, br, 12_500, D2, t0 + 86_400)
    assert br.buys == []                         # 어제 미확정 봉으로 주문하지 않는다


def test_reconfigure_locks_zone_after_first_buy():
    eng, br, _ = make()
    eng.legs[0].status = "filled"
    eng.legs[0].est_qty = 10
    with pytest.raises(ValueError):
        eng.reconfigure(CandleTradeConfig(total_krw=1_000_000, zone_margin_pct=20))
    eng.reconfigure(CandleTradeConfig(total_krw=2_000_000, take_profit_pct=30))
    assert eng.legs[1].amount == 1_000_000 and eng.config.take_profit_pct == 30


async def test_fill_event_replaces_estimate():
    eng, br, _ = make()
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)
    eng.on_fill("buy", 40, 11_100)
    eng.on_fill("buy", 4, 11_200)
    assert eng.legs[0].qty == 44
    assert eng.legs[0].price == pytest.approx((40 * 11_100 + 4 * 11_200) / 44)


async def test_roundtrip_persistence():
    eng, br, _ = make()
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)
    await tick(eng, br, 13_200, D1)
    data = eng.to_dict()
    back = CandleTradeEngine.from_dict("000001", eng.config, data)
    assert back.phase == CzPhase.TRAILING and back.shares == eng.shares
    assert back.zone() == eng.zone() and back.peak == eng.peak and back.base_confirmed
    assert back.day == ""                         # 하루 맥락은 재시작 후 다시 받는다
