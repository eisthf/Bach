"""대금 양봉 매매 전략 엔진.

거래대금 상위 양봉 조회에서 고른 종목을, 조회일의 **기준 캔들**(큰 양봉) 몸통 안쪽
'매입 구간'에서 여러 거래일에 걸쳐 2분할 매수하고, 절반 익절 + 트레일링 스톱으로
청산한다. 하루로 끝나지 않으므로 상태 전체를 직렬화(``to_dict``/``from_dict``)해
재시작 후에도 이어간다.

매입 구간 (몸통 = 종가 − 시가, x = zone_margin_pct):
  상단 = 종가 − 몸통 × x%,   하단 = 시가 + 몸통 × x%

구간 매매(kind="zone"): 같은 엔진을 임의 종목에 쓰되, 매입 구간 상단·하단을 사람이
직접 정한다(예: 10일선 눌림목). 기준 캔들이 없으므로 '기준일 다음 거래일부터' 대신
**감시를 시작한 거래일부터** 평가하고, 1차 기한도 그날부터 센다(``watch_from``).
나머지(2차 기준·절반 익절·트레일링·구간 이탈 손절)는 모두 같다.

흐름 (기준일 D 당일에는 아무것도 평가하지 않는다 — D+1 거래일부터):
  DRAFT ──[감시 시작]→ WATCHING
  WATCHING:
    - 가격이 매입 구간 안 → 1차 매수(총 투자액 × 1차 비중) → ACCUMULATING
    - D+entry_days 거래일까지 1차 조건이 없으면 → DONE(기한 만료, 매매 안 함)
  ACCUMULATING (1차만 보유, 2차 대기):
    - 2차: 1차 매수일 **다음 날부터**, 가격이 매입 구간 안이면서 2차 조건가 이하
           → 나머지 금액 매수 → HOLDING. 조건가(P=1차 매수가, L·H=구간 하단·상단, add_mode):
             fixed  P × (1 − u%)
             gap    H − k% × (H − L)  (구간 상단에서 구간 폭의 k% 아래 = 하단에서 (100−k)% 위.
                    1차가 대비 하락폭 < 최소 하락률이면 2차 없음 — 1차가 그 가격 근처나 아래에서
                    체결됐을 때 같은 가격에 금액만 두 배로 사는 것을 막는다)
             mixed  (P − L)/P > u% 일 때만 min(fixed, H − k% × (H − L)) — 둘 중 더 깊이
                    내려간 가격 (기본). 아니면 2차 없음. 구간이 넓을 때 −u% 고정은 너무 위에서
                    사게 되므로 하단 쪽에서 사되, 여유가 빠듯해도 최소 u%는 내려간 곳에서 산다
  ACCUMULATING / HOLDING:
    - 익절: 가격 ≥ 평단 × (1 + p%) → 남은 매수 계획 취소 후
        trailing=True(기본)  보유의 절반 매도 → TRAILING
        trailing=False       전량 매도 → DONE
  TRAILING: 고점 × (1 − t%) 이하 → 나머지 전량 매도 → DONE
            (이미 TRAILING이면 도중에 trailing을 꺼도 끝까지 트레일링한다 — 절반은 이미 팔았다)
  구간 이탈 손절 (ACCUMULATING / HOLDING / TRAILING, 1차 매수 다음 날부터):
    손절선 = 매입 구간 하단 × (1 − m%) (m=stop_margin_pct, 기본 0 = 하단 그대로).
    전일 종가 < 손절선이면, 그날 가격이 손절선 아래로 내려오는 순간 전량 매도 → DONE
  MANUAL: 사람이 수동매매로 인계받음(엔진 정지)

새 구간(재진입, 구간 매매만 — ``rezone``): 보유 중이고 익절 전(ACCUMULATING/HOLDING)이면 매입 구간을
다시 정해 새 '회차'를 시작할 수 있다(예: 손절선 근처로 내려왔지만 20일선 반등을 노릴 때).
  - 대기 중이던 차수는 취소하고, 지금 보유(수량·평단)는 이월분(carry)으로 그대로 둔다.
  - 새 구간에서 새 1차·2차 계획(금액 = 회차 예산, 기본은 취소된 차수 금액)을 세운다. 새 1차는
    가격이 새 구간에 들어오면 바로(그날 포함), 기한 없이 — 이미 보유 중이라 끝낼 수 없다.
  - 평단 = (이월 원가 + 새 체결) / 전체 수량. 익절은 이 평단 기준, 구간 이탈 손절은 새 구간 기준.

'가격'은 감시 단위 설정에 따른다 — 틱(기본)이면 틱마다, 분이면 완성된 N분봉의
종가(구간의 마지막 틱)로 평가한다.

하루치 정보(전일 종가, 기준일 이후 경과 거래일)는 엔진이 스스로 알 수 없어 호출측이
매 세션 첫 평가 전에 ``begin_day``로 넣는다(일봉 조회 결과).

주문은 콜백(awaitable)으로 위임한다 — buy_fn(amount) -> 추정 체결가(실패 0),
sell_fn(qty) -> 실제 주문 수량(실패 0). 장 시간 판정은 호출측 책임이다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Awaitable, Callable, List, Optional, Tuple

from ..models import CandleTradeConfig, Tick

BuyFn = Callable[[int], Awaitable[float]]
SellFn = Callable[[int], Awaitable[int]]


class CzPhase(str, Enum):
    DRAFT = "DRAFT"                  # 설정 중. 주문 없음
    WATCHING = "WATCHING"            # 매입 구간 진입(1차 매수) 대기
    ACCUMULATING = "ACCUMULATING"    # 1차 보유, 2차 매수 대기
    HOLDING = "HOLDING"              # 2차까지 보유(또는 2차 실패), 익절 대기
    TRAILING = "TRAILING"            # 절반 익절 후 나머지 트레일링 스톱
    DONE = "DONE"                    # 청산 완료(또는 기한 만료 등으로 종료)
    MANUAL = "MANUAL"                # 수동매매 인계

    @property
    def active(self) -> bool:
        return self in (CzPhase.WATCHING, CzPhase.ACCUMULATING,
                        CzPhase.HOLDING, CzPhase.TRAILING)

    @property
    def holding(self) -> bool:
        return self in (CzPhase.ACCUMULATING, CzPhase.HOLDING, CzPhase.TRAILING)


@dataclass
class CzLeg:
    ratio: float                     # 총 투자액 대비 비율(%)
    amount: int                      # 배정 금액(원)
    status: str = "waiting"          # waiting | filled | failed | canceled
    est_qty: int = 0                 # 주문 응답 기준 잠정 수량
    est_price: float = 0.0           # 주문 응답 기준 잠정 체결가(직전 틱)
    fill_qty: int = 0                # 체결 이벤트 누적 수량
    fill_avg: float = 0.0            # 체결 이벤트 평균 체결가
    date: str = ""                   # 체결 세션 날짜(YYYY-MM-DD)

    @property
    def qty(self) -> int:
        return self.fill_qty if self.fill_qty else self.est_qty

    @property
    def price(self) -> float:
        """이 차수의 체결가 — 실체결 이벤트가 있으면 그 평균, 없으면 잠정값."""
        return self.fill_avg if self.fill_qty else self.est_price


_BASIS = {"fixed": "고정 하락률", "gap": "구간 폭 비율", "": ""}


def add_trigger_for(config: CandleTradeConfig, first_price: float,
                    zone: Optional[Tuple[float, float]]) -> Tuple[Optional[float], str]:
    """2차 매수 조건가 (가격, 근거). 근거 "fixed"=1차가 −u%, "gap"=구간 상단에서 구간 폭의 k% 아래."""
    p = first_price
    if p <= 0:
        return None, ""
    fixed = p * (1 - config.add_drop_pct / 100)
    low, high = zone if zone else (None, None)
    band = high - (high - low) * config.add_gap_pct / 100 if zone else None
    if config.add_mode == "fixed":
        return fixed, "fixed"
    if config.add_mode == "gap":
        ok = band is not None and (p - band) / p * 100 >= config.add_min_drop_pct
        return (band, "gap") if ok else (None, "")
    # mixed: 1차가에서 하단까지 여유가 u% 를 넘을 때만, 고정·구간 비율 중 더 깊은 가격에서
    if low is None or p <= low or (p - low) / p * 100 <= config.add_drop_pct:
        return None, ""
    return (band, "gap") if band < fixed else (fixed, "fixed")


def build_legs(config: CandleTradeConfig) -> List[CzLeg]:
    first = config.first_ratio_pct
    return [CzLeg(ratio=r, amount=int(config.total_krw * r / 100)) for r in (first, 100 - first)]


@dataclass
class CandleTradeEngine:
    code: str
    config: CandleTradeConfig
    # 기준 캔들(조회일의 양봉)
    base_date: str = ""
    base_open: float = 0.0
    base_close: float = 0.0
    # 기준일 일봉을 실제 차트 데이터로 확인했는가(장중 스냅샷으로 등록했을 수 있다).
    base_confirmed: bool = False
    # "candle"=대금 양봉 매매(기준 캔들로 구간 계산) | "zone"=구간 매매(구간을 사람이 지정)
    kind: str = "candle"
    zone_low: float = 0.0
    zone_high: float = 0.0
    # 구간 매매의 경과 거래일 기준(감시를 시작한 첫 세션 날짜). 감시 중지 시 비운다.
    watch_from: str = ""
    log: Callable[[str], None] = field(default=lambda _: None, repr=False)
    # 수동매매 인계 알림(사유). 호출측이 인계 시점을 오래 남는 기록에 적는다.
    on_handoff: Callable[[str], None] = field(default=lambda _: None, repr=False)

    phase: CzPhase = CzPhase.DRAFT
    legs: List[CzLeg] = field(default_factory=list)
    sold_qty: int = 0                # 엔진이 낸 매도 수량 누계
    peak: float = 0.0                # 트레일링 고점
    exit_reason: str = ""
    # 체결 이벤트를 귀속시킬 차수(방금 주문한 차수). 영속화한다 — 재시작 직후
    # 늦게 도착한 체결도 올바른 차수에 붙도록.
    active_leg: Optional[int] = None
    # 지금 내는 주문의 사유("1차"·"절반 익절"·청산 사유). buy_fn/sell_fn 직전에 적어 두면 호출측이
    # 주문번호에 붙여 체결 기록에 남긴다(영속화하지 않음).
    order_reason: str = field(default="", repr=False)
    # 하루치 맥락(begin_day). day 가 오늘이 아니면 평가하지 않는다.
    day: str = ""
    days_elapsed: int = 0            # 기준일(구간 매매는 감시 시작일 전날) 이후 경과 거래일(오늘 포함)
    prev_close: Optional[float] = None
    stop_armed: bool = False         # 오늘 구간 이탈 손절이 켜져 있는가
    # 새 구간(재진입) 회차. 이월분 = 앞 회차들에서 넘어온 보유 수량과 그 원가 합계.
    round_no: int = 1
    round_budget: int = 0            # 이번 회차 예산(0 = 설정의 총 투자액 — 첫 회차)
    carry_qty: int = 0
    carry_cost: float = 0.0
    # 분 단위 감시 버퍼(영속화하지 않음)
    _bar_bucket: Optional[int] = field(default=None, repr=False)
    _bar_close: Optional[float] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.legs:
            self.legs = build_legs(self.config)

    # -- 파생 값 ------------------------------------------------------------
    @property
    def bought_qty(self) -> int:
        return sum(leg.qty for leg in self.legs if leg.status == "filled")

    @property
    def shares(self) -> int:
        return max(0, self.carry_qty + self.bought_qty - self.sold_qty)

    @property
    def avg_cost(self) -> float:
        """평단 = (이월 원가 + 이번 회차 체결 원가) / 전체 매수 수량. 매도는 평단을 바꾸지 않는다."""
        filled = [leg for leg in self.legs if leg.status == "filled" and leg.qty > 0]
        qty = self.carry_qty + sum(leg.qty for leg in filled)
        cost = self.carry_cost + sum(leg.price * leg.qty for leg in filled)
        return cost / qty if qty else 0.0

    @property
    def pending_amount(self) -> int:
        """아직 안 산 차수 금액 합계(새 구간 회차 예산의 기본값)."""
        return sum(leg.amount for leg in self.legs if leg.status == "waiting")

    @property
    def first_date(self) -> str:
        return self.legs[0].date if self.legs and self.legs[0].status == "filled" else ""

    def zone(self) -> Optional[Tuple[float, float]]:
        """매입 구간 (하단, 상단). 기준 캔들이 양봉이 아니거나 구간 미지정이면 None."""
        if self.kind == "zone":
            ok = 0 < self.zone_low < self.zone_high
            return (self.zone_low, self.zone_high) if ok else None
        body = self.base_close - self.base_open
        if self.base_open <= 0 or body <= 0:
            return None
        margin = body * self.config.zone_margin_pct / 100
        return self.base_open + margin, self.base_close - margin

    def in_zone(self, price: float) -> bool:
        z = self.zone()
        return z is not None and z[0] <= price <= z[1]

    def add_plan(self) -> Tuple[Optional[float], str]:
        """2차 매수 조건가와 그 근거("fixed" | "gap" | ""). 1차 전이거나 2차가 끝났으면
        (None, ""). 조건가가 구간 하단 아래면 2차는 나가지 않는다(구간 안이어야 하므로)."""
        if len(self.legs) < 2 or self.legs[0].status != "filled" or self.legs[1].status != "waiting":
            return None, ""
        return add_trigger_for(self.config, self.legs[0].price, self.zone())

    def add_trigger(self) -> Optional[float]:
        return self.add_plan()[0]

    def add_possible(self) -> bool:
        trigger, z = self.add_trigger(), self.zone()
        return trigger is not None and z is not None and trigger >= z[0]

    def stop_line(self) -> Optional[float]:
        """구간 이탈 손절선 = 구간 하단 × (1 − m%)."""
        z = self.zone()
        return z[0] * (1 - self.config.stop_margin_pct / 100) if z else None

    def take_profit_price(self) -> Optional[float]:
        if self.phase not in (CzPhase.ACCUMULATING, CzPhase.HOLDING):
            return None
        avg = self.avg_cost
        return avg * (1 + self.config.take_profit_pct / 100) if avg > 0 else None

    def trailing_stop(self) -> Optional[float]:
        if self.phase != CzPhase.TRAILING or self.peak <= 0:
            return None
        return self.peak * (1 - self.config.trailing_pct / 100)

    # -- 설정 ----------------------------------------------------------------
    def set_base(self, date: str, open_: float, close: float, confirmed: bool) -> None:
        self.base_date, self.base_open, self.base_close = date, float(open_), float(close)
        self.base_confirmed = confirmed

    @property
    def bought(self) -> bool:
        return self.carry_qty > 0 or any(leg.status == "filled" for leg in self.legs)

    def _new_legs(self, config: CandleTradeConfig) -> List[CzLeg]:
        if self.round_budget <= 0:
            return build_legs(config)
        first = config.first_ratio_pct
        return [CzLeg(ratio=r, amount=int(self.round_budget * r / 100)) for r in (first, 100 - first)]

    def rezone(self, low: float, high: float, budget: int) -> None:
        """새 구간(재진입). 대기 차수를 취소하고 보유는 이월, 새 구간에서 ``budget``원으로
        새 1차·2차 계획을 세운다. 손절선이 새 구간 기준으로 바뀌므로 오늘 손절 여부도 다시 판단."""
        if self.kind != "zone":
            raise ValueError("새 구간 설정은 구간 매매에서만 할 수 있습니다.")
        if self.phase not in (CzPhase.ACCUMULATING, CzPhase.HOLDING) or self.shares <= 0:
            raise ValueError("보유 중이고 익절 전(1차·2차 보유)일 때만 새 구간을 설정할 수 있습니다.")
        low, high, budget = float(low), float(high), int(budget)
        if not 0 < low < high:
            raise ValueError("매입 구간은 0 < 하단 < 상단 이어야 합니다.")
        if budget < 10_000:
            raise ValueError("새 구간 매수 금액은 1만원 이상이어야 합니다.")
        canceled = self.pending_amount
        qty, avg = self.shares, self.avg_cost
        self.carry_qty, self.carry_cost = qty, avg * qty
        self.sold_qty = 0
        self.zone_low, self.zone_high = low, high
        self.round_no += 1
        self.round_budget = budget
        self.legs = self._new_legs(self.config)
        self.active_leg = None        # 앞 회차 주문의 늦은 체결이 새 차수에 붙지 않게
        self.phase = CzPhase.ACCUMULATING
        if self.day:
            self._arm_stop(self.day)
        self.log(f"새 구간 {self.round_no}회차: {low:,.0f} ~ {high:,.0f}, 매수 {budget:,}원"
                 f"(취소한 대기 차수 {canceled:,}원) — 보유 {qty}주 평단 {avg:,.0f} 이월, "
                 f"손절선 {self.stop_line():,.0f}" + (" · 오늘 손절 켜짐" if self.stop_armed else ""))

    def set_zone(self, low: float, high: float) -> None:
        """구간 매매의 매입 구간 지정. 1차 매수 뒤엔 바꿀 수 없다(평단·손절 기준)."""
        if self.kind != "zone":
            raise ValueError("대금 양봉 매매의 구간은 기준 캔들로 정해집니다.")
        if self.bought:
            raise ValueError("1차 매수 후에는 매입 구간을 바꿀 수 없습니다.")
        low, high = float(low), float(high)
        if not 0 < low < high:
            raise ValueError("매입 구간은 0 < 하단 < 상단 이어야 합니다.")
        self.zone_low, self.zone_high = low, high
        self.log(f"매입 구간 지정 {low:,.0f} ~ {high:,.0f}")

    def reconfigure(self, config: CandleTradeConfig) -> None:
        """설정 변경. 1차 매수 전엔 차수 계획을 다시 짜고, 그 뒤엔 1차 비중은 고정하고
        남은 차수 금액·조건값만 바꾼다. 매입 구간 폭은 매수 시작 후 바꿀 수 없다
        (평단·손절 기준이 흔들린다)."""
        if not any(leg.status == "filled" for leg in self.legs):
            self.config = config
            self.legs = self._new_legs(config)
            return
        if abs(config.first_ratio_pct - self.config.first_ratio_pct) > 1e-9:
            raise ValueError("1차 매수 후에는 1차 비중을 바꿀 수 없습니다.")
        if self.kind == "candle" and abs(config.zone_margin_pct - self.config.zone_margin_pct) > 1e-9:
            raise ValueError("1차 매수 후에는 매입 구간을 바꿀 수 없습니다.")
        if config.total_krw != self.config.total_krw and self.round_budget <= 0:
            for leg in self.legs:
                if leg.status == "waiting":
                    leg.amount = int(config.total_krw * leg.ratio / 100)
        if (config.watch_mode, config.watch_minutes) != (self.config.watch_mode, self.config.watch_minutes):
            self._reset_bar()
        self.config = config

    # -- 시작·중지·인계 ------------------------------------------------------
    def start(self) -> None:
        if self.phase != CzPhase.DRAFT:
            raise ValueError("설정 중인 종목만 감시를 시작할 수 있습니다.")
        if self.zone() is None:
            raise ValueError("매입 구간 상단·하단을 먼저 지정하세요." if self.kind == "zone"
                             else "기준 캔들이 양봉이 아니라 매입 구간을 정할 수 없습니다.")
        self.phase = CzPhase.WATCHING
        self.day = ""
        self.watch_from = ""
        lo, hi = self.zone()
        since = "감시 시작일부터" if self.kind == "zone" else f"기준일({self.base_date}) 이후"
        self.log(f"감시 시작 — 매입 구간 {lo:,.0f} ~ {hi:,.0f}, "
                 f"{since} {self.config.entry_days}거래일 안에 진입")

    def stop(self) -> bool:
        """1차 매수 전 감시 중지(설정 중으로 되돌림)."""
        if self.phase != CzPhase.WATCHING:
            return False
        self.phase = CzPhase.DRAFT
        self.day = ""
        self.watch_from = ""
        self.log("감시 중지")
        return True

    def hand_off(self, reason: str = "수동 전환") -> None:
        self.phase = CzPhase.MANUAL
        self.exit_reason = reason
        self.log(f"수동매매 인계: {reason}")
        try:
            self.on_handoff(reason)
        except Exception:  # noqa: BLE001 — 기록 실패가 인계를 막지 않게
            pass

    # -- 하루 맥락 -----------------------------------------------------------
    def needs_day(self, today: str) -> bool:
        return self.phase.active and self.day != today

    def begin_day(self, today: str, prev_close: Optional[float], days_elapsed: int) -> None:
        """세션 첫 평가 전에 호출. ``days_elapsed``는 기준일 이후 경과 거래일(오늘 포함,
        기준일 당일이면 0)."""
        self.day = today
        self.days_elapsed = days_elapsed
        self.prev_close = prev_close
        self.stop_armed = False
        self._reset_bar()
        if days_elapsed <= 0:
            return
        if self.phase == CzPhase.WATCHING and days_elapsed > self.config.entry_days:
            self.phase = CzPhase.DONE
            since = "감시 시작일부터" if self.kind == "zone" else "기준일 이후"
            self.exit_reason = f"기한 만료: {since} {self.config.entry_days}거래일 안에 매입 구간 진입 없음"
            self.log(self.exit_reason)
            return
        self._arm_stop(today)

    def _arm_stop(self, today: str) -> None:
        """오늘 구간 이탈 손절을 켤지. 이월분이 있으면(새 구간 회차) 이미 며칠 보유한 셈이라
        '1차 다음 날부터' 조건 없이 전일 종가만 본다."""
        line, prev_close = self.stop_line(), self.prev_close
        held_before = self.carry_qty > 0 or (self.first_date and self.first_date < today)
        self.stop_armed = bool(self.phase.holding and self.shares > 0 and line and held_before
                               and prev_close is not None and prev_close < line)
        if self.stop_armed:
            self.log(f"전일 종가 {prev_close:,.0f} < {self._stop_label()} {line:,.0f} — "
                     f"오늘 그 아래 가격에서 전량 매도")

    # -- 틱 평가 -------------------------------------------------------------
    async def on_tick(self, tick: Tick, today: str, buy_fn: BuyFn, sell_fn: SellFn) -> None:
        """정규장 틱 1개. 호출측이 장중에, ``begin_day(today, …)`` 뒤에 호출한다."""
        if tick.price <= 0 or not self.phase.active or self.day != today or self.days_elapsed <= 0:
            return
        if self.config.watch_mode == "tick":
            await self._evaluate(tick.price, today, buy_fn, sell_fn)
            return
        # 분 단위: 다음 구간의 첫 틱이 와야 직전 봉 종가가 확정된다(ULC 3분봉과 같은 방식).
        if tick.time <= 0:
            return
        bucket = tick.time // (self.config.watch_minutes * 60)
        if self._bar_bucket is None or bucket > self._bar_bucket:
            completed = self._bar_close if self._bar_bucket is not None else None
            self._bar_bucket, self._bar_close = bucket, tick.price
            if completed is not None:
                await self._evaluate(completed, today, buy_fn, sell_fn)
        elif bucket == self._bar_bucket:
            self._bar_close = tick.price
        # bucket < 현재: 재연결 등으로 늦게 온 과거 틱 — 무시

    def _reset_bar(self) -> None:
        self._bar_bucket = None
        self._bar_close = None

    async def _evaluate(self, price: float, today: str, buy_fn: BuyFn, sell_fn: SellFn) -> None:
        z = self.zone()
        if z is None:
            return
        lo, hi = z
        c = self.config

        if self.phase == CzPhase.WATCHING:
            if lo <= price <= hi:
                if await self._buy(0, price, today, buy_fn):
                    self.phase = CzPhase.ACCUMULATING
                    trigger, basis = self.add_plan()
                    add = (f"2차 매수: 다음 거래일부터 구간 안 & ≤ {trigger:,.0f}({_BASIS[basis]})"
                           if self.add_possible() else "2차 매수 없음(1차가에서 구간 하단까지 여유 부족)")
                    self.log(f"{add}, {'절반 익절' if c.trailing else '전량 익절'}: 평단 +{c.take_profit_pct:g}%")
                else:
                    self.phase = CzPhase.DONE
                    self.exit_reason = "1차 매수 실패"
                    self.log("1차 매수 실패 — 종료(설정·예수금을 확인하고 다시 등록하세요)")
            return

        if self.shares <= 0:
            return

        line = self.stop_line()
        if self.stop_armed and line and price < line:
            await self._exit(sell_fn, f"구간 이탈 손절: 전일 종가 < {self._stop_label()}, "
                                      f"{line:,.0f} 아래 {price:,.0f}")
            return

        if self.phase == CzPhase.TRAILING:
            self.peak = max(self.peak, price)
            stop = self.trailing_stop()
            if stop is not None and price <= stop:
                await self._exit(sell_fn, f"트레일링 스톱: 고점 {self.peak:,.0f} −{c.trailing_pct:g}%")
            return

        avg = self.avg_cost
        if avg > 0 and price >= avg * (1 + c.take_profit_pct / 100):
            await self._take_half(price, sell_fn)
            return

        if self.phase == CzPhase.ACCUMULATING and self.legs[0].status != "filled":
            # 새 구간 회차의 1차: 새 구간에 들어오면 바로(기한 없음)
            if self.legs[0].status == "waiting" and lo <= price <= hi:
                if await self._buy(0, price, today, buy_fn):
                    trigger, basis = self.add_plan()
                    self.log(f"{self.round_no}회차 2차 매수: " + (
                        f"다음 거래일부터 구간 안 & ≤ {trigger:,.0f}({_BASIS[basis]})" if self.add_possible()
                        else "없음(1차가에서 구간 하단까지 여유 부족)"))
                else:
                    self.phase = CzPhase.HOLDING
                    self.log(f"{self.round_no}회차 1차 매수 실패 — 추가매수 중단, 보유분 익절·손절 감시")
            return

        if self.phase == CzPhase.ACCUMULATING:
            trigger = self.add_trigger()
            if (trigger is not None and self.first_date < today
                    and lo <= price <= hi and price <= trigger):
                if await self._buy(1, price, today, buy_fn):
                    self.phase = CzPhase.HOLDING
                else:
                    # 실패한 차수는 재시도하지 않는다(틱마다 주문이 반복되지 않게).
                    self.phase = CzPhase.HOLDING
                    self.log("2차 매수 실패 — 추가매수 중단, 보유분 익절·손절 감시")

    async def _buy(self, i: int, price: float, today: str, buy_fn: BuyFn) -> bool:
        leg = self.legs[i]
        self.active_leg = i
        self.order_reason = f"{i + 1}차"
        est = await buy_fn(leg.amount)
        if est <= 0:
            leg.status = "failed"
            return False
        leg.status = "filled"
        leg.date = today
        leg.est_price = est
        leg.est_qty = int(leg.amount // est)
        self.log(f"{i + 1}차 매수 {leg.qty}주 @ {leg.price:,.0f} (감시가 {price:,.0f}, 평단 {self.avg_cost:,.0f})")
        return True

    def _stop_label(self) -> str:
        m = self.config.stop_margin_pct
        return f"하단 −{m:g}%" if m > 0 else "하단"

    def _cancel_waiting(self) -> None:
        for leg in self.legs:
            if leg.status == "waiting":
                leg.status = "canceled"

    async def _take_half(self, price: float, sell_fn: SellFn) -> None:
        c = self.config
        half = self.shares // 2
        if not c.trailing:     # 트레일링 없이 익절가에서 전량 매도로 끝
            self._cancel_waiting()
            await self._exit(sell_fn, f"익절 +{c.take_profit_pct:g}% (전량)")
            return
        if half <= 0:      # 1주뿐이면 절반이 없다 — 전량 익절
            self._cancel_waiting()
            await self._exit(sell_fn, f"익절 +{c.take_profit_pct:g}% (1주라 전량)")
            return
        self.order_reason = "절반 익절"
        sold = await sell_fn(half)
        if sold <= 0:
            self.hand_off("절반 익절 매도 실패 — 계좌에서 확인 필요")
            return
        self.sold_qty += sold
        self._cancel_waiting()
        self.phase = CzPhase.TRAILING
        self.peak = price
        self.log(f"절반 익절 +{c.take_profit_pct:g}%: {sold}주 매도, 남은 매수 계획 취소 → "
                 f"나머지 {self.shares}주 트레일링(고점 −{c.trailing_pct:g}%)")

    async def _exit(self, sell_fn: SellFn, reason: str) -> None:
        qty = self.shares
        self.order_reason = reason
        sold = await sell_fn(qty)
        self.sold_qty += max(0, sold)
        self._cancel_waiting()
        self.phase = CzPhase.DONE
        self.exit_reason = reason
        self.log(f"{reason}: 전량 {qty}주 매도" + ("" if sold > 0 else " 실패"))

    # -- 체결 이벤트 ---------------------------------------------------------
    def on_fill(self, side: str, qty: int, price: float) -> None:
        """실시간 체결(00) — 방금 주문한 차수의 실체결 수량·평균가를 누적한다."""
        if side != "buy" or qty <= 0 or price <= 0 or self.active_leg is None:
            return
        leg = self.legs[self.active_leg]
        total = leg.fill_qty + qty
        leg.fill_avg = (leg.fill_avg * leg.fill_qty + price * qty) / total
        leg.fill_qty = total
        if leg.status == "waiting":   # 주문 응답보다 체결 이벤트가 먼저 온 경우
            leg.status = "filled"

    # -- 영속화 --------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "phase": self.phase.value,
            "base": {"date": self.base_date, "open": self.base_open, "close": self.base_close,
                     "confirmed": self.base_confirmed},
            "kind": self.kind,
            "zone": {"low": self.zone_low, "high": self.zone_high},
            "watch_from": self.watch_from,
            "legs": [asdict(leg) for leg in self.legs],
            "sold_qty": self.sold_qty,
            "peak": self.peak,
            "exit_reason": self.exit_reason,
            "active_leg": self.active_leg,
            "round": {"no": self.round_no, "budget": self.round_budget,
                      "carry_qty": self.carry_qty, "carry_cost": self.carry_cost},
        }

    @classmethod
    def from_dict(cls, code: str, config: CandleTradeConfig, data: dict,
                  log: Callable[[str], None] = lambda _: None) -> "CandleTradeEngine":
        base = data.get("base") or {}
        zone = data.get("zone") or {}
        rnd = data.get("round") or {}
        legs = [CzLeg(**leg) for leg in data.get("legs") or []] or build_legs(config)
        return cls(
            code=code, config=config, log=log,
            base_date=str(base.get("date") or ""),
            base_open=float(base.get("open") or 0), base_close=float(base.get("close") or 0),
            base_confirmed=bool(base.get("confirmed")),
            kind=str(data.get("kind") or "candle"),
            zone_low=float(zone.get("low") or 0), zone_high=float(zone.get("high") or 0),
            watch_from=str(data.get("watch_from") or ""),
            phase=CzPhase(data.get("phase", CzPhase.DRAFT.value)),
            legs=legs,
            sold_qty=int(data.get("sold_qty") or 0),
            peak=float(data.get("peak") or 0),
            exit_reason=data.get("exit_reason", ""),
            active_leg=data.get("active_leg"),
            round_no=int(rnd.get("no") or 1), round_budget=int(rnd.get("budget") or 0),
            carry_qty=int(rnd.get("carry_qty") or 0), carry_cost=float(rnd.get("carry_cost") or 0),
        )
