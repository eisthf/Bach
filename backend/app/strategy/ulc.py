"""상한가 따라잡기(upper_limit_chase) 전략 엔진.

`/home/rblue/work/kiwoom/doc/상한가 전략 시각화.md`의 흐름을 틱 기반으로 구현.
mock 모드에서 AUTO_TRADING 상태의 종목에 대해 틱마다 ``on_tick``이 호출되어
분할매수·익절·손절·트레일링을 시뮬레이션한다.

흐름 요약:
  X = 전일 종가(상한가), Z = 당일 시가
  - 진입필터: Z >= X*(1+w) → SKIP, X < 1000 → SKIP, 하락시가 & !allow_lower_open → SKIP
  - 시나리오: SC1 [X,X*(1+p)) / SC2 [X*(1+p),X*(1+p1)) / SC3 [X*(1+p1),∞)
  - 분할매수: SC1/2 = 2분할, SC3 = 3분할
  - 청산: 평단*(1+tp) 익절, 평단*(1-sl) 손절(분할 완료 후 활성), 트레일링(옵션)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional

from ..models import AutoConfig, Tick


class Phase(str, Enum):
    INIT = "INIT"            # 진입 판단 전
    SKIPPED = "SKIPPED"      # 진입필터 탈락
    ACCUMULATING = "ACCUMULATING"  # 분할매수 진행 중
    HOLDING = "HOLDING"      # 매수 완료, 청산 대기
    TRAILING = "TRAILING"    # 반익절 후 트레일링
    DONE = "DONE"            # 청산 완료


@dataclass
class BuyLeg:
    target: float            # 매수 목표가
    amount: int              # 배정 금액(원)
    filled: bool = False


@dataclass
class UlcEngine:
    """종목 1개의 ULC 매매 상태기.

    Broker 의존성을 직접 갖지 않고, 콜백(buy_fn/sell_fn)으로 주문을 위임한다.
    이렇게 하면 mock/live 양쪽에서 동일 엔진을 재사용할 수 있다.
    buy_fn(amount_krw) -> 체결가(없으면 0), sell_fn(qty) -> 체결가
    """
    code: str
    config: AutoConfig
    x: float                 # 전일 종가(상한가)
    z: float                 # 당일 시가
    log: Callable[[str], None]

    phase: Phase = Phase.INIT
    scenario: int = 0
    legs: List[BuyLeg] = field(default_factory=list)
    avg_cost: float = 0.0
    shares: int = 0
    half_sold: bool = False
    trail_max: float = 0.0

    # ------------------------------------------------------------------
    def _emit(self, msg: str) -> None:
        self.log(f"[{self.code}] {msg}")

    def setup(self) -> None:
        """진입필터 + 시나리오 분류 + 분할매수 계획 수립."""
        c = self.config
        x, z = self.x, self.z

        if x < 1_000:
            self.phase = Phase.SKIPPED
            self._emit(f"SKIP: 저가주 X={x:,.0f} < 1,000")
            return
        if z >= x * (1 + c.ulc_w):
            self.phase = Phase.SKIPPED
            self._emit(f"SKIP: 갭 과도 Z={z:,.0f} >= X*(1+w)={x*(1+c.ulc_w):,.0f}")
            return
        if z < x and not c.ulc_allow_lower_open:
            self.phase = Phase.SKIPPED
            self._emit(f"SKIP: 하락 시가 Z={z:,.0f} < X={x:,.0f}")
            return

        # 시나리오 분류
        if z < x * (1 + c.ulc_p):
            self.scenario = 1
        elif z < x * (1 + c.ulc_p1):
            self.scenario = 2
        else:
            self.scenario = 3

        amt = c.max_buy_amount
        if self.scenario in (1, 2):
            half = amt // 2
            y = (z * 0)  # placeholder
            if self.scenario == 1:
                y = x * (1 - c.ulc_q)
            else:  # SC2
                y = x
            self.legs = [BuyLeg(target=z, amount=half), BuyLeg(target=y, amount=amt - half)]
        else:  # SC3
            third = amt // 3
            self.legs = [
                BuyLeg(target=z, amount=third),
                BuyLeg(target=z * 0.95, amount=third),
                BuyLeg(target=z * 0.9025, amount=amt - 2 * third),
            ]

        if c.ulc_first_buy_only:
            self.legs = self.legs[:1]

        self.phase = Phase.ACCUMULATING
        targets = ", ".join(f"{leg.target:,.0f}" for leg in self.legs)
        self._emit(f"SC{self.scenario} 진입. 분할매수 목표: [{targets}]")

    # ------------------------------------------------------------------
    @property
    def all_filled(self) -> bool:
        return all(leg.filled for leg in self.legs)

    def on_tick(
        self,
        tick: Tick,
        buy_fn: Callable[[int], float],
        sell_fn: Callable[[int], float],
    ) -> None:
        """틱 1개 처리."""
        if self.phase in (Phase.INIT, Phase.SKIPPED, Phase.DONE):
            return
        price = tick.price
        c = self.config

        # 1) 분할매수: 1차는 즉시(시가), 이후는 목표가 도달 시
        if self.phase == Phase.ACCUMULATING:
            for i, leg in enumerate(self.legs):
                if leg.filled:
                    continue
                hit = (i == 0) or (price <= leg.target)
                if hit:
                    fill = buy_fn(leg.amount)
                    if fill > 0:
                        qty = int(leg.amount // fill)
                        if qty > 0:
                            new_shares = self.shares + qty
                            self.avg_cost = (self.avg_cost * self.shares + fill * qty) / new_shares
                            self.shares = new_shares
                            leg.filled = True
                            self._emit(f"{i+1}차 매수 {qty}주 @ {fill:,.0f} (평단 {self.avg_cost:,.0f})")
                    else:
                        leg.filled = True  # 체결 실패해도 무한루프 방지
                break  # 한 틱에 한 단계만
            if self.all_filled:
                self.phase = Phase.HOLDING
                self._emit("분할매수 완료 → HOLDING")

        if self.shares <= 0:
            return

        # 손절 활성 조건: 마지막 분할 매수 체결 이후
        stop_active = self.all_filled

        # 2) 트레일링 모드
        if self.phase == Phase.TRAILING:
            self.trail_max = max(self.trail_max, price)
            # 손절 우선
            if price <= self.avg_cost * (1 - c.ulc_sl):
                self._exit_all(sell_fn, "트레일링 중 손절")
                return
            # 상한가 도달
            if price >= self.x * 1.295:
                self._exit_all(sell_fn, "상한가 도달")
                return
            # 보장 익절
            if price >= self.avg_cost * (1 + c.ulc_g):
                self._exit_all(sell_fn, "보장 익절")
                return
            # 트레일링 스탑
            if price <= self.trail_max * (1 - c.ulc_t):
                self._exit_all(sell_fn, f"트레일링 스탑 (max {self.trail_max:,.0f})")
                return
            return

        # 3) HOLDING: 익절/손절
        if self.phase == Phase.HOLDING:
            if stop_active and price <= self.avg_cost * (1 - c.ulc_sl):
                self._exit_all(sell_fn, "손절")
                return
            if price >= self.avg_cost * (1 + c.ulc_tp):
                if c.ulc_trailing:
                    half = self.shares // 2
                    if half > 0:
                        fill = sell_fn(half)
                        self.shares -= half
                        self.half_sold = True
                        self._emit(f"익절: 절반 {half}주 매도 @ {fill:,.0f} → 트레일링 가동")
                    self.trail_max = price
                    self.phase = Phase.TRAILING
                else:
                    self._exit_all(sell_fn, "익절 전량")
                return

    def _exit_all(self, sell_fn: Callable[[int], float], reason: str) -> None:
        if self.shares > 0:
            fill = sell_fn(self.shares)
            self._emit(f"{reason}: 전량 {self.shares}주 매도 @ {fill:,.0f}")
            self.shares = 0
        self.phase = Phase.DONE
