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
    """기본 흐름 테스트는 2차 조건가가 단순한 고정 −5%로 쓴다(add_mode 별 동작은 아래 따로)."""
    logs = []
    cfg.setdefault("add_mode", "fixed")
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


def test_trailing_is_on_by_default():
    assert CandleTradeConfig().trailing is True
    assert CandleTradeConfig.model_validate({"total_krw": 1_000_000}).trailing is True   # 예전 저장값


async def test_without_trailing_sells_all_at_take_profit():
    eng, br, logs = make(trailing=False)
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)          # 45주 @ 11,000, 익절가 13,200
    assert any("전량 익절" in m for m in logs)
    await tick(eng, br, 13_199, D1)
    assert br.sells == []
    await tick(eng, br, 13_200, D1)
    assert br.sells == [45] and eng.phase == CzPhase.DONE and eng.shares == 0
    assert eng.legs[1].status == "canceled" and "전량" in eng.exit_reason
    assert eng.trailing_stop() is None


async def test_turning_trailing_off_while_trailing_keeps_trailing():
    eng, br, _ = make()
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)
    await tick(eng, br, 13_200, D1)          # 절반 익절 → TRAILING
    eng.reconfigure(CandleTradeConfig(total_krw=1_000_000, add_mode="fixed", trailing=False))
    await tick(eng, br, 14_000, D1)
    assert br.sells == [22] and eng.phase == CzPhase.TRAILING
    await tick(eng, br, 13_300, D1)
    assert br.sells == [22, 23] and eng.phase == CzPhase.DONE


def test_take_profit_mode_defaults_to_pct_and_line_needs_price():
    assert CandleTradeConfig().take_profit_mode == "pct"
    assert CandleTradeConfig.model_validate({"total_krw": 1_000_000}).take_profit_mode == "pct"   # 예전 저장값
    with pytest.raises(ValueError, match="익절 라인"):
        CandleTradeConfig(take_profit_mode="line")


async def test_take_profit_by_price_line():
    eng, br, logs = make(take_profit_mode="line", take_profit_line=12_500)
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)          # 45주 @ 11,000 — % 기준이면 13,200
    assert eng.take_profit_price() == 12_500 and any("라인 12,500 이상" in m for m in logs)
    await tick(eng, br, 12_499, D1)
    assert br.sells == []
    await tick(eng, br, 12_500, D1)          # 라인 포함
    assert br.sells == [22] and eng.phase == CzPhase.TRAILING
    assert any("라인 12,500" in m for m in logs)


async def test_take_profit_line_with_full_exit():
    eng, br, _ = make(take_profit_mode="line", take_profit_line=12_000, trailing=False)
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)
    await tick(eng, br, 12_100, D1)
    assert br.sells == [45] and eng.phase == CzPhase.DONE and "라인 12,000" in eng.exit_reason


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


async def test_zone_break_stop_with_margin_below_low():
    eng, br, logs = make(stop_margin_pct=2)  # 하단 10,200 × 0.98 = 손절선 9,996
    assert eng.stop_line() == pytest.approx(9_996)
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)
    day(eng, D2, 2, prev_close=10_100)       # 하단 아래지만 손절선 위 마감 → 손절 꺼짐
    assert not eng.stop_armed
    await tick(eng, br, 9_900, D2)
    assert br.sells == []
    day(eng, D3, 3, prev_close=9_990)        # 손절선 아래 마감 → 오늘 손절선 아래 틱에서 전량
    assert eng.stop_armed and any("하단 −2%" in m for m in logs)
    await tick(eng, br, 10_000, D3)          # 하단 아래라도 손절선 위면 팔지 않는다
    assert br.sells == []
    await tick(eng, br, 9_995, D3)
    assert eng.phase == CzPhase.DONE and "하단 −2%" in eng.exit_reason


def test_stop_mode_defaults_to_close_confirmation():
    assert CandleTradeConfig().stop_mode == "close"
    assert CandleTradeConfig.model_validate({"total_krw": 1_000_000}).stop_mode == "close"   # 예전 저장값


async def test_instant_stop_sells_on_first_touch_even_on_buy_day():
    eng, br, logs = make(stop_mode="instant", stop_margin_pct=2)   # 손절선 9,996
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)          # 1차
    await tick(eng, br, 9_996, D1)           # 손절선 그 자체는 '아래'가 아니다
    assert br.sells == [] and not eng.stop_armed
    await tick(eng, br, 9_995, D1)           # 1차 당일이라도 즉시 전량
    assert br.sells == [45] and eng.phase == CzPhase.DONE and "즉시" in eng.exit_reason


async def test_instant_stop_ignores_prev_close_rule():
    eng, br, _ = make(stop_mode="instant")
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)
    day(eng, D2, 2, prev_close=10_100)       # 종가 확인 방식이면 켜질 날 — 즉시 방식은 arm 없이 가격만 본다
    assert not eng.stop_armed
    await tick(eng, br, 10_200, D2)          # 하단 그대로 → 매도 없음(2차 조건엔 맞음)
    assert br.sells == []
    await tick(eng, br, 10_199, D2)
    assert eng.phase == CzPhase.DONE


async def test_close_stop_ignores_intraday_break_on_unarmed_day():
    eng, br, _ = make()                      # 기본 = 종가 확인
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)
    day(eng, D2, 2, prev_close=10_300)
    await tick(eng, br, 9_000, D2)           # 장중 크게 이탈해도 그날은 팔지 않는다
    assert br.sells == [] and eng.phase != CzPhase.DONE


def test_stop_margin_defaults_to_zero():
    assert CandleTradeConfig().stop_margin_pct == 0
    eng, _, _ = make()
    assert eng.stop_line() == pytest.approx(10_200)


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


# ---------------------------------------------------------------------------
# 2차 조건가 기준: fixed / gap / mixed(기본)
# ---------------------------------------------------------------------------
def make_small(**cfg):
    """몸통 5% 캔들: 시가 10,000 → 종가 10,500, 구간 10,050 ~ 10,450 (고정 −5%는 구간 밖)."""
    eng, br, logs = make(**cfg)
    eng.set_base(BASE, 10_000, 10_500, confirmed=True)
    return eng, br, logs


def test_default_add_mode_is_mixed():
    assert CandleTradeConfig().add_mode == "mixed"


async def test_mixed_buys_low_in_wide_zone():
    eng, br, _ = make(add_mode="mixed")
    day(eng, D1, 1)
    await tick(eng, br, 11_500, D1)          # 여유 11.3% > 5% → 구간 비율 11,800 − 0.8×1,600 = 10,520
    assert eng.add_plan() == (pytest.approx(10_520), "gap") and eng.add_possible()
    day(eng, D2, 2)
    await tick(eng, br, 10_925, D2)          # 고정 −5% 가격에서는 사지 않는다
    assert len(br.buys) == 1
    await tick(eng, br, 10_520, D2)
    assert len(br.buys) == 2 and eng.phase == CzPhase.HOLDING


@pytest.mark.parametrize("first", [11_200, 11_500, 11_800])
async def test_band_price_depends_on_zone_not_first_price(first):
    eng, br, _ = make(add_mode="gap", add_gap_pct=80)
    day(eng, D1, 1)
    await tick(eng, br, first, D1)           # 구간 10,200~11,800: 상단에서 폭의 80% 아래 = 10,520
    assert eng.add_plan() == (pytest.approx(10_520), "gap")


async def test_mixed_uses_fixed_when_band_price_is_shallower():
    eng, br, _ = make(add_mode="mixed")
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)          # 구간 비율 10,520(−4.4%) vs 고정 10,450(−5%) → 더 깊은 고정
    assert eng.add_plan() == (pytest.approx(10_450), "fixed")


async def test_mixed_skips_second_leg_when_room_within_threshold():
    eng, br, logs = make_small(add_mode="mixed")
    day(eng, D1, 1)
    await tick(eng, br, 10_400, D1)          # 하단(10,050)까지 여유 3.4% ≤ 5% → 2차 없음
    assert eng.add_plan() == (None, "") and not eng.add_possible()
    assert any("2차 매수 없음" in m for m in logs)
    day(eng, D2, 2)
    await tick(eng, br, 10_050, D2)
    assert len(br.buys) == 1


@pytest.mark.parametrize("first, ok", [(10_600, False), (10_700, True)])
async def test_mixed_threshold_around_five_percent(first, ok):
    eng, br, _ = make(add_mode="mixed")
    eng.set_base(BASE, 10_000, 11_000, confirmed=True)   # 구간 10,100 ~ 10,900
    day(eng, D1, 1)
    await tick(eng, br, first, D1)           # 여유 4.7% → 2차 없음, 5.6% → 2차 있음
    assert eng.add_possible() is ok


async def test_mixed_uses_fixed_when_gap_price_is_shallower():
    eng, br, _ = make(add_mode="mixed")
    eng.set_base(BASE, 10_000, 11_000, confirmed=True)   # 구간 10,100 ~ 10,900
    day(eng, D1, 1)
    await tick(eng, br, 10_700, D1)          # 구간 비율 10,260(−4.1%) vs 고정 10,165(−5%) → 더 깊은 고정
    assert eng.add_plan() == (pytest.approx(10_165), "fixed")
    day(eng, D2, 2)
    await tick(eng, br, 10_200, D2)
    assert len(br.buys) == 1
    await tick(eng, br, 10_165, D2)
    assert len(br.buys) == 2


async def test_gap_below_min_drop_means_no_second_leg():
    eng, br, logs = make_small(add_mode="gap")
    day(eng, D1, 1)
    await tick(eng, br, 10_100, D1)          # 구간 비율 10,130은 1차가보다 위 → 하락 < 최소 2%
    assert not eng.add_possible()
    day(eng, D2, 2)
    await tick(eng, br, 10_050, D2)
    assert len(br.buys) == 1


async def test_fixed_mode_keeps_unreachable_trigger():
    eng, br, _ = make_small(add_mode="fixed")
    day(eng, D1, 1)
    await tick(eng, br, 10_400, D1)
    assert eng.add_plan() == (pytest.approx(9_880), "fixed") and not eng.add_possible()
    day(eng, D2, 2)
    await tick(eng, br, 10_100, D2)
    assert len(br.buys) == 1


async def test_gap_mode_on_big_candle_goes_deeper():
    eng, br, _ = make(add_mode="gap", add_gap_pct=80)
    day(eng, D1, 1)
    await tick(eng, br, 11_500, D1)          # 11,800 − 0.8×1,600 = 10,520 (고정 −5%면 10,925)
    assert eng.add_plan() == (pytest.approx(10_520), "gap")
    day(eng, D2, 2)
    await tick(eng, br, 10_600, D2)
    assert len(br.buys) == 1
    await tick(eng, br, 10_520, D2)
    assert len(br.buys) == 2


# ---------------------------------------------------------------------------
# 구간 매매(kind="zone"): 매입 구간을 사람이 지정
# ---------------------------------------------------------------------------
def make_zone(low=10_200, high=11_800, **cfg):
    logs = []
    cfg.setdefault("add_mode", "fixed")
    eng = CandleTradeEngine("000001", CandleTradeConfig(total_krw=1_000_000, **cfg),
                            kind="zone", log=logs.append)
    if low:
        eng.set_zone(low, high)
    return eng, Broker(), logs


def test_zone_kind_requires_zone_before_start():
    eng, _, _ = make_zone(low=0)
    assert eng.zone() is None
    with pytest.raises(ValueError, match="상단·하단"):
        eng.start()
    with pytest.raises(ValueError):
        eng.set_zone(11_000, 10_000)          # 하단 ≥ 상단


async def test_zone_kind_trades_from_first_watch_day():
    eng, br, _ = make_zone()
    eng.start()
    day(eng, D1, 1)                           # 감시 시작일 = 1거래일째 — 바로 평가
    await tick(eng, br, 11_000, D1)
    assert br.buys == [500_000] and eng.zone() == (10_200, 11_800)
    with pytest.raises(ValueError):           # 1차 후엔 구간 고정
        eng.set_zone(10_000, 11_000)
    eng.reconfigure(CandleTradeConfig(total_krw=1_000_000, add_mode="fixed", zone_margin_pct=30))
    day(eng, D2, 2)
    await tick(eng, br, 10_450, D2)           # 2차 규칙은 대금 양봉과 같다
    assert len(br.buys) == 2


async def test_zone_kind_expiry_message():
    eng, br, _ = make_zone(entry_days=2)
    eng.start()
    day(eng, D3, 3)
    assert eng.phase == CzPhase.DONE and "감시 시작일부터" in eng.exit_reason


# ---------------------------------------------------------------------------
# 새 구간(재진입): 대기 차수 취소, 보유 이월, 새 구간에서 새 1차·2차
# ---------------------------------------------------------------------------
async def zone_with_first_leg():
    """구간 10,200~11,800, 1차 50만원을 11,000에 45주. 2차 50만원 대기."""
    eng, br, logs = make_zone(add_mode="mixed")
    eng.start()
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)
    assert eng.shares == 45 and eng.pending_amount == 500_000
    return eng, br, logs


async def test_rezone_carries_position_and_plans_new_round():
    eng, br, logs = await zone_with_first_leg()
    eng.rezone(9_000, 9_400, eng.pending_amount)
    assert eng.round_no == 2 and eng.phase == CzPhase.ACCUMULATING
    assert eng.zone() == (9_000, 9_400) and eng.stop_line() == pytest.approx(9_000)
    assert eng.shares == 45 and eng.avg_cost == pytest.approx(11_000)       # 이월
    assert [leg.amount for leg in eng.legs] == [250_000, 250_000]           # 회차 예산 50만원
    assert all(leg.status == "waiting" for leg in eng.legs)
    assert any("2회차" in m and "45주" in m for m in logs)
    day(eng, D2, 2, prev_close=9_500)
    await tick(eng, br, 9_500, D2)            # 새 구간 위 — 매수 없음
    assert len(br.buys) == 1
    await tick(eng, br, 9_400, D2)            # 새 구간 진입 → 새 1차(그날 바로)
    assert br.buys[1:] == [250_000] and eng.legs[0].qty == 26
    assert eng.avg_cost == pytest.approx((45 * 11_000 + 26 * 9_400) / 71)
    # 2차: 1차가 9,400 → 하단 9,000까지 4.3% ≤ 5% → 2차 없음(혼합)
    assert not eng.add_possible()


async def test_rezone_take_profit_uses_combined_average():
    eng, br, _ = await zone_with_first_leg()
    eng.rezone(9_000, 9_400, 500_000)
    day(eng, D2, 2, prev_close=9_500)
    await tick(eng, br, 9_200, D2)            # 새 1차 27주 @ 9,200
    avg = eng.avg_cost
    assert avg == pytest.approx((45 * 11_000 + 27 * 9_200) / 72)
    assert eng.take_profit_price() == pytest.approx(avg * 1.2)
    await tick(eng, br, avg * 1.2 - 1, D2)
    assert br.sells == []
    await tick(eng, br, avg * 1.2 + 1, D2)    # 합산 평단 +20% → 보유 전체의 절반
    assert br.sells == [36] and eng.phase == CzPhase.TRAILING
    assert eng.legs[1].status == "canceled"


async def test_rezone_stop_uses_new_zone_and_arms_without_new_buy():
    eng, br, _ = await zone_with_first_leg()
    eng.rezone(9_000, 9_400, 500_000)
    day(eng, D2, 2, prev_close=8_900)         # 이월분이 있으니 새 1차 전이라도 손절 판단
    assert eng.stop_armed
    await tick(eng, br, 9_100, D2)            # 새 구간 안이지만 손절이 먼저? → 손절선 위라 매수
    assert len(br.buys) == 2
    await tick(eng, br, 8_990, D2)
    assert eng.phase == CzPhase.DONE and br.sells == [45 + 27] and "구간 이탈" in eng.exit_reason


async def test_rezone_mid_session_rearms_stop_with_new_line():
    eng, br, _ = await zone_with_first_leg()
    day(eng, D2, 2, prev_close=10_100)        # 하단 10,200 아래 마감 → 오늘 손절 켜짐
    assert eng.stop_armed
    eng.rezone(9_000, 9_400, 500_000)         # 장중 새 구간: 전일 종가 10,100 ≥ 새 손절선 → 꺼짐
    assert not eng.stop_armed
    await tick(eng, br, 9_900, D2)
    assert br.sells == [] and eng.phase == CzPhase.ACCUMULATING


async def test_rezone_after_second_leg_and_budget_override():
    eng, br, _ = await zone_with_first_leg()
    day(eng, D2, 2)
    eng.legs[1].status = "filled"              # 2차까지 산 상태로 만든다
    eng.legs[1].est_price, eng.legs[1].est_qty = 10_450, 47
    eng.phase = CzPhase.HOLDING
    assert eng.pending_amount == 0
    eng.rezone(9_000, 9_400, 800_000)          # 못 쓴 돈이 없으니 금액을 직접 준다
    assert eng.shares == 92 and [leg.amount for leg in eng.legs] == [400_000, 400_000]


async def test_rezone_rejected_when_not_holding_or_trailing():
    eng, br, _ = make_zone()
    eng.start()
    with pytest.raises(ValueError, match="보유 중"):
        eng.rezone(9_000, 9_400, 500_000)      # 1차 전
    eng, br, _ = await zone_with_first_leg()
    await tick(eng, br, 13_200, D1)            # 절반 익절 → TRAILING
    with pytest.raises(ValueError, match="보유 중"):
        eng.rezone(9_000, 9_400, 500_000)
    eng2, _, _ = make()
    with pytest.raises(ValueError, match="구간 매매"):
        eng2.rezone(9_000, 9_400, 500_000)


async def test_rezone_roundtrip_and_reconfigure_keeps_round_budget():
    eng, br, _ = await zone_with_first_leg()
    eng.rezone(9_000, 9_400, 600_000)
    back = CandleTradeEngine.from_dict("000001", eng.config, eng.to_dict())
    assert (back.round_no, back.round_budget, back.carry_qty) == (2, 600_000, 45)
    assert back.avg_cost == pytest.approx(11_000) and back.shares == 45
    back.reconfigure(CandleTradeConfig(total_krw=3_000_000, add_mode="mixed", first_ratio_pct=40))
    assert [leg.amount for leg in back.legs] == [240_000, 360_000]   # 회차 예산 기준


async def test_rezone_again_before_any_fill_keeps_round_and_carry():
    eng, br, logs = await zone_with_first_leg()
    eng.rezone(9_000, 9_400, 500_000)
    eng.rezone(8_800, 9_300, 700_000)          # 2회차에 아직 체결 없음 → 회차 유지, 구간·금액만
    assert eng.round_no == 2 and eng.zone() == (8_800, 9_300)
    assert (eng.carry_qty, eng.avg_cost) == (45, pytest.approx(11_000))
    assert [leg.amount for leg in eng.legs] == [350_000, 350_000]
    assert any("회차 유지" in m for m in logs)
    day(eng, D2, 2, prev_close=9_500)
    await tick(eng, br, 9_200, D2)             # 2회차 1차 체결
    eng.rezone(8_000, 8_500, 500_000)          # 체결이 있었으니 이제 3회차, 보유 다시 이월
    assert eng.round_no == 3 and eng.carry_qty == 45 + 38
    assert eng.avg_cost == pytest.approx((45 * 11_000 + 38 * 9_200) / 83)


async def test_round_budget_editable_only_before_new_first_leg():
    eng, br, _ = await zone_with_first_leg()
    with pytest.raises(ValueError, match="새 구간"):
        eng.set_round_budget(800_000)          # 1회차는 총 투자액
    eng.rezone(9_000, 9_400, 500_000)
    eng.set_round_budget(800_000)
    assert eng.round_budget == 800_000 and [leg.amount for leg in eng.legs] == [400_000, 400_000]
    with pytest.raises(ValueError, match="1만원"):
        eng.set_round_budget(5_000)
    day(eng, D2, 2, prev_close=9_500)
    await tick(eng, br, 9_200, D2)             # 새 1차 체결
    with pytest.raises(ValueError, match="1차 매수 후"):
        eng.set_round_budget(600_000)


def test_zone_kind_roundtrip():
    eng, _, _ = make_zone()
    eng.watch_from = D1
    back = CandleTradeEngine.from_dict("000001", eng.config, eng.to_dict())
    assert back.kind == "zone" and back.zone() == (10_200, 11_800) and back.watch_from == D1


# ---------------------------------------------------------------------------
# 한 번에 매수(분할 끔)
# ---------------------------------------------------------------------------
def test_split_is_on_by_default():
    assert CandleTradeConfig().split is True
    assert CandleTradeConfig.model_validate({"total_krw": 1_000_000}).split is True   # 예전 저장값


async def test_single_buy_spends_total_at_once_and_holds():
    eng, br, logs = make(split=False)
    assert [(leg.ratio, leg.amount) for leg in eng.legs] == [(100.0, 1_000_000)]
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)
    assert br.buys == [1_000_000] and eng.phase == CzPhase.HOLDING and eng.shares == 90
    assert not eng.add_possible() and any("한 번에 매수" in m for m in logs)
    day(eng, D2, 2)
    await tick(eng, br, 10_300, D2)           # 구간 안 깊은 가격이어도 추가 매수 없음
    assert br.buys == [1_000_000]
    await tick(eng, br, 13_200, D2)           # 익절: 평단 +20% → 절반 매도 후 트레일링
    assert br.sells == [45] and eng.phase == CzPhase.TRAILING


async def test_split_toggle_only_before_first_buy():
    eng, br, _ = make()
    eng.reconfigure(eng.config.model_copy(update={"split": False}))
    assert [leg.amount for leg in eng.legs] == [1_000_000]
    eng.reconfigure(eng.config.model_copy(update={"split": True}))
    assert [leg.amount for leg in eng.legs] == [500_000, 500_000]
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)
    with pytest.raises(ValueError, match="분할"):
        eng.reconfigure(eng.config.model_copy(update={"split": False}))


async def test_single_buy_rezone_round_is_single_too():
    eng, br, _ = make_zone(split=False)
    eng.start()
    day(eng, D1, 1)
    await tick(eng, br, 11_000, D1)
    eng.rezone(9_000, 9_400, 300_000)
    assert [leg.amount for leg in eng.legs] == [300_000]
    day(eng, D2, 2, prev_close=9_500)
    await tick(eng, br, 9_400, D2)
    assert br.buys == [1_000_000, 300_000] and eng.phase == CzPhase.HOLDING
