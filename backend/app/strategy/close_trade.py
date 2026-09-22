"""종가 매매(close trade) 전략 엔진.

거래대금 상위 양봉 조회에서 고른 종목을 **여러 거래일에 걸쳐** 분할매수·청산한다.
상한가 전략(ULC)과 달리 하루로 끝나지 않으므로, 장 마감에 초기화되지 않고
상태 전체를 직렬화(``to_dict``/``from_dict``)해 재시작 후에도 이어간다.

흐름:
  DRAFT ──[1차 매수]──┬─ 장중: 즉시 1차 시장가 → WAIT_NEXT_DAY(당일은 감시 안 함)
                      └─ 장외: PENDING_OPEN → 다음 장 첫 틱에 1차 시장가 → 곧바로 감시
  WAIT_NEXT_DAY ─(다음 거래일 첫 틱)→ ACCUMULATING(남은 차수 있음) | HOLDING
  ACCUMULATING:
    - 조기 익절: 현재가 ≥ 평단×(1+early_tp) → 전량 매도, 남은 차수 취소 → DONE
    - 다음 차수: 현재가 ≤ 직전 차수 체결가×(1−drop) → 시장가 매수(틱당 한 차수)
    - 마지막 차수 체결 → HOLDING
  HOLDING (분할 완료 — 손절은 여기서만 켠다. 분할 중의 하락은 추가매수 신호다):
    - 익절: 현재가 ≥ 평단×(1+tp) → 전량 매도 → DONE
    - 손절: 현재가 ≤ 평단×(1−sl) → 전량 매도 → DONE
  MANUAL: 사람이 수동매매로 인계받음(엔진 정지)

기준가:
  차수별 기준은 **실체결가**다. 시장가 주문 응답의 가격은 직전 틱 가격일 뿐이라,
  체결(00) 이벤트가 오면 그 차수의 평균 체결가로 대체한다(``on_fill``). 이벤트가
  없는 환경(mock)에서는 주문 시점 가격을 잠정값으로 쓴다.

주문은 콜백(awaitable)으로 위임한다 — buy_fn(amount) -> 추정 체결가(실패 0),
sell_fn(qty) -> 추정 체결가(실패 0). 장 시간 판정은 호출측(Hub) 책임이다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Awaitable, Callable, List, Optional

from ..models import CloseTradeConfig, Tick


class CtPhase(str, Enum):
    DRAFT = "DRAFT"                  # 설정 중. 주문 없음
    PENDING_OPEN = "PENDING_OPEN"    # 1차 매수 예약(다음 장 시가)
    WAIT_NEXT_DAY = "WAIT_NEXT_DAY"  # 1차 체결, 다음 거래일부터 감시
    ACCUMULATING = "ACCUMULATING"    # 추가 차수 감시 중
    HOLDING = "HOLDING"              # 분할 완료, 익절/손절 감시
    DONE = "DONE"                    # 청산 완료(또는 진입 불가로 종료)
    MANUAL = "MANUAL"                # 수동매매 인계

    @property
    def active(self) -> bool:
        return self in (CtPhase.PENDING_OPEN, CtPhase.WAIT_NEXT_DAY,
                        CtPhase.ACCUMULATING, CtPhase.HOLDING)


@dataclass
class CtLeg:
    ratio: float                     # 총 투자액 대비 비율(%)
    amount: int                      # 배정 금액(원)
    status: str = "waiting"          # waiting | filled | failed
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


def build_legs(config: CloseTradeConfig) -> List[CtLeg]:
    return [CtLeg(ratio=r, amount=int(config.total_krw * r / 100)) for r in config.ratios]


@dataclass
class CloseTradeEngine:
    code: str
    config: CloseTradeConfig
    log: Callable[[str], None] = field(default=lambda _: None, repr=False)

    phase: CtPhase = CtPhase.DRAFT
    legs: List[CtLeg] = field(default_factory=list)
    # 이 세션 날짜에는 추가매수·청산을 평가하지 않는다(장중 1차 매수 당일).
    quiet_date: str = ""
    exit_reason: str = ""
    # 체결 이벤트를 귀속시킬 차수(방금 주문한 차수). 영속화한다 — 재시작 직후
    # 늦게 도착한 체결도 올바른 차수에 붙도록.
    active_leg: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.legs:
            self.legs = build_legs(self.config)

    # -- 파생 값 ------------------------------------------------------------
    @property
    def shares(self) -> int:
        return sum(leg.qty for leg in self.legs if leg.status == "filled")

    @property
    def avg_cost(self) -> float:
        filled = [leg for leg in self.legs if leg.status == "filled" and leg.qty > 0]
        qty = sum(leg.qty for leg in filled)
        return sum(leg.price * leg.qty for leg in filled) / qty if qty else 0.0

    def next_leg(self) -> Optional[int]:
        return next((i for i, leg in enumerate(self.legs) if leg.status == "waiting"), None)

    def trigger_price(self, i: int) -> Optional[float]:
        """i차(1-based 아님, 0-based index) 매수 조건가. 1차(0)는 조건 없음."""
        if i <= 0 or i >= len(self.legs):
            return None
        prev = self.legs[i - 1]
        if prev.status != "filled" or prev.price <= 0:
            return None
        return prev.price * (1 - self.config.add_drop_pcts[i - 1] / 100)

    # -- 설정 ----------------------------------------------------------------
    def reconfigure(self, config: CloseTradeConfig) -> None:
        """설정 변경. 주문 전(DRAFT/PENDING_OPEN)엔 차수 계획을 다시 짜고,
        진입 후엔 이미 체결된 차수는 그대로 두고 조건값만 바꾼다(차수 수·비율 고정)."""
        if self.phase in (CtPhase.DRAFT, CtPhase.PENDING_OPEN):
            self.config = config
            self.legs = build_legs(config)
            return
        if len(config.ratios) != len(self.legs) or any(
                abs(a - b.ratio) > 1e-9 for a, b in zip(config.ratios, self.legs)):
            raise ValueError("매수 시작 후에는 분할 비율·차수 수를 바꿀 수 없습니다.")
        if config.total_krw != self.config.total_krw:
            for leg, r in zip(self.legs, config.ratios):
                if leg.status == "waiting":
                    leg.amount = int(config.total_krw * r / 100)
        self.config = config

    # -- 진입 ----------------------------------------------------------------
    async def enter(self, price: float, today: str, market_open: bool,
                    buy_fn: Callable[[int], Awaitable[float]]) -> bool:
        """[1차 매수] 버튼. 장중이면 즉시 시장가, 장외면 다음 장 시가로 예약."""
        if self.phase != CtPhase.DRAFT:
            return False
        if not market_open:
            self.phase = CtPhase.PENDING_OPEN
            self.log("1차 매수 예약: 다음 장 시가(첫 체결)에 시장가 매수 후 곧바로 감시")
            return True
        if not await self._buy(0, price, today, buy_fn):
            self.log("1차 매수 실패 — 설정 상태로 되돌림")
            self.phase = CtPhase.DRAFT
            self.legs = build_legs(self.config)
            return False
        self.quiet_date = today
        self.phase = CtPhase.WAIT_NEXT_DAY
        self.log("다음 거래일부터 추가매수·익절·손절 감시")
        return True

    def cancel_pending(self) -> bool:
        if self.phase != CtPhase.PENDING_OPEN:
            return False
        self.phase = CtPhase.DRAFT
        self.log("1차 매수 예약 취소")
        return True

    def hand_off(self, reason: str = "수동 전환") -> None:
        self.phase = CtPhase.MANUAL
        self.exit_reason = reason
        self.log(f"수동매매 인계: {reason}")

    # -- 틱 평가 -------------------------------------------------------------
    async def on_tick(self, tick: Tick, today: str,
                      buy_fn: Callable[[int], Awaitable[float]],
                      sell_fn: Callable[[int], Awaitable[float]]) -> None:
        """정규장 틱 1개 평가. 호출측이 장중에만 호출한다."""
        price = tick.price
        if price <= 0 or not self.phase.active:
            return

        if self.phase == CtPhase.PENDING_OPEN:
            if await self._buy(0, price, today, buy_fn):
                self.quiet_date = ""
                self.phase = self._after_fill_phase()
                self.log("시가 1차 매수 완료 → 곧바로 감시 시작")
            else:
                self.phase = CtPhase.DONE
                self.exit_reason = "1차 매수 실패"
                self.log("시가 1차 매수 실패 — 종료(설정을 확인하고 다시 등록하세요)")
            return

        if self.phase == CtPhase.WAIT_NEXT_DAY:
            if today <= self.quiet_date:
                return
            self.phase = self._after_fill_phase()
            self.log(f"감시 시작({today}) — 평단 {self.avg_cost:,.0f}")

        if self.shares <= 0:
            return
        avg = self.avg_cost
        c = self.config

        if self.phase == CtPhase.ACCUMULATING:
            if price >= avg * (1 + c.early_tp_pct / 100):
                await self._exit(sell_fn, f"조기 익절 +{c.early_tp_pct:g}% (남은 차수 취소)")
                return
            i = self.next_leg()
            if i is None:
                self.phase = CtPhase.HOLDING
                return
            trigger = self.trigger_price(i)
            if trigger is not None and price <= trigger:
                if await self._buy(i, price, today, buy_fn):
                    if self.next_leg() is None:
                        self.phase = CtPhase.HOLDING
                        self.log(f"분할매수 완료 → 익절 +{c.tp_pct:g}% / 손절 −{c.sl_pct:g}% 감시")
                else:
                    # 실패한 차수는 재시도하지 않는다. 다음 차수 기준가(이 차수
                    # 체결가)가 없으므로 추가매수를 멈추고 보유분만 관리한다.
                    for leg in self.legs:
                        if leg.status == "waiting":
                            leg.status = "failed"
                    self.phase = CtPhase.HOLDING
                    self.log(f"{i + 1}차 매수 실패 — 추가매수 중단, 보유분 익절/손절 감시")
            return

        if self.phase == CtPhase.HOLDING:
            if price >= avg * (1 + c.tp_pct / 100):
                await self._exit(sell_fn, f"익절 +{c.tp_pct:g}%")
            elif price <= avg * (1 - c.sl_pct / 100):
                await self._exit(sell_fn, f"손절 −{c.sl_pct:g}%")

    def _after_fill_phase(self) -> CtPhase:
        return CtPhase.ACCUMULATING if self.next_leg() is not None else CtPhase.HOLDING

    async def _buy(self, i: int, price: float, today: str,
                   buy_fn: Callable[[int], Awaitable[float]]) -> bool:
        leg = self.legs[i]
        self.active_leg = i
        est = await buy_fn(leg.amount)
        if est <= 0:
            leg.status = "failed"
            return False
        leg.status = "filled"
        leg.date = today
        leg.est_price = est
        leg.est_qty = int(leg.amount // est)
        self.log(f"{i + 1}차 매수 {leg.qty}주 @ {leg.price:,.0f} (평단 {self.avg_cost:,.0f})")
        return True

    async def _exit(self, sell_fn: Callable[[int], Awaitable[float]], reason: str) -> None:
        qty = self.shares
        await sell_fn(qty)
        self.phase = CtPhase.DONE
        self.exit_reason = reason
        self.log(f"{reason}: 전량 {qty}주 매도")

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
            "legs": [asdict(leg) for leg in self.legs],
            "quiet_date": self.quiet_date,
            "exit_reason": self.exit_reason,
            "active_leg": self.active_leg,
        }

    @classmethod
    def from_dict(cls, code: str, config: CloseTradeConfig, data: dict,
                  log: Callable[[str], None] = lambda _: None) -> "CloseTradeEngine":
        legs = [CtLeg(**leg) for leg in data.get("legs") or []] or build_legs(config)
        return cls(
            code=code, config=config, log=log,
            phase=CtPhase(data.get("phase", CtPhase.DRAFT.value)),
            legs=legs,
            quiet_date=data.get("quiet_date", ""),
            exit_reason=data.get("exit_reason", ""),
            active_leg=data.get("active_leg"),
        )
