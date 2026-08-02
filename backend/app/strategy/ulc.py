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
from typing import Awaitable, Callable, List, Optional

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

    ⚠️ 두 콜백은 **awaitable** 이다. 실거래 주문은 블로킹 HTTP(+rate-limit
    대기)라, 호출측이 이를 스레드로 넘겨 이벤트 루프를 막지 않게 하기 위함이다.
    엔진의 판단 로직 자체는 루프 위에서 동기적으로 돌아 상태 경쟁이 없다.
    """
    code: str
    config: AutoConfig
    x: float                 # 전일 종가(상한가)
    z: float                 # 당일 시가
    log: Callable[[str], None]
    # 계좌 실보유 조회(awaitable) → (수량, 평단) 또는 None(조회 불가/미지원).
    # 주문 콜백이 돌려주는 체결가는 시장가 주문의 '직전 틱 가격'이라 실체결가가
    # 아니다. 이 콜백으로 평단·수량을 계좌 기준에 맞춰 보정한다.
    position_fn: Optional[Callable[[], Awaitable[Optional[tuple]]]] = None

    phase: Phase = Phase.INIT
    scenario: int = 0
    legs: List[BuyLeg] = field(default_factory=list)
    avg_cost: float = 0.0
    shares: int = 0
    # 평단이 계좌로 확인됐는가. 매수 때마다 False 로 돌아가고, 계좌가 체결을
    # 반영해 수량이 일치하는 순간 True 가 된다(그 전까지 매 틱 재시도).
    _avg_synced: bool = False
    # 00(주문체결) 이벤트로 확인된 실체결 누적. 계좌 폴링(kt00018)보다 빠르고,
    # 이 엔진이 낸 주문만 반영하므로 기존 보유분이 섞이지 않는다.
    fill_qty: int = 0
    fill_avg: float = 0.0
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

    def on_fill(self, side: str, qty: int, price: float) -> None:
        """실시간 체결(00) 반영 — 평단을 실체결가로 즉시 확정.

        주문 REST 응답에는 체결가가 없고(주문번호만), 계좌 평단은 폴링이라
        몇 틱 늦는다. 이 이벤트는 단위체결가(FID 914)를 실시간으로 주므로
        가장 빠르고 정확한 경로다. 계좌 평단과 달리 이 엔진이 낸 주문만
        누적되어 기존 보유분이 섞이지 않는다.

        동기 메서드다 — WS 디스패치에서 곧바로 호출되며 I/O 가 없다.
        """
        if qty <= 0 or self.phase in (Phase.INIT, Phase.SKIPPED):
            return
        if side == "buy":
            if price <= 0:
                return
            total = self.fill_qty + qty
            self.fill_avg = (self.fill_avg * self.fill_qty + price * qty) / total
            self.fill_qty = total
        else:
            # 매도는 수량만 줄인다(주당 평단은 변하지 않는다).
            self.fill_qty = max(0, self.fill_qty - qty)
        if self.fill_qty > 0:
            if abs(self.fill_avg - self.avg_cost) >= 1:
                self._emit(f"평단 확정(실체결): {self.avg_cost:,.0f} → {self.fill_avg:,.0f}")
            self.avg_cost = self.fill_avg
            self._avg_synced = True

    async def _sync_from_account(self) -> None:
        """보유수량·평단을 계좌(실보유) 기준으로 보정한다.

        엔진이 자체 추정한 수량/평단은 두 가지 이유로 틀어진다:
          1. 시장가 주문의 체결가를 알 수 없어 '직전 틱 가격'으로 평단을 계산
          2. 부분체결·주문거부 시 추정 수량이 실보유보다 많아짐

        보정 규칙(보수적):
        - 계좌 수량이 추정보다 **적으면** 실보유에 맞춘다(과다 매도 방지).
        - 계좌 수량이 추정보다 **많으면** 건드리지 않는다. 자동매매 이전부터
          갖고 있던 물량(보유 종목 가져오기 등)일 수 있어, 엔진이 남의 물량까지
          팔지 않게 한다.
        - 수량이 정확히 일치할 때만 평단을 계좌 값으로 채택한다. 불일치 상태의
          계좌 평단은 외부 물량이 섞인 값이라 익절·손절 기준으로 부적절하다.
        - 계좌가 0을 보고하면 무시한다. 주문 직후 계좌(kt00018)가 아직 체결을
          반영하지 못한 것일 수 있어, 0을 믿으면 보유를 놓친다.
        """
        if self.position_fn is None:
            return
        try:
            pos = await self.position_fn()
        except Exception:  # noqa: BLE001  (조회 실패 시 추정치 유지)
            return
        if not pos:
            return
        qty, avg = int(pos[0]), float(pos[1] or 0.0)
        if qty <= 0:
            return
        if qty < self.shares:
            self._emit(
                f"⚠️ 계좌 잔량 {qty}주 < 엔진 추정 {self.shares}주 "
                f"— 부분체결/미체결로 보고 실보유에 맞춤"
            )
            self.shares = qty
        # 실체결(00) 로 확정된 평단이 있으면 그쪽이 우선이다. 계좌 평단은
        # 기존 보유분까지 섞인 집계값이라 이 엔진의 손익 기준으로는 부정확하다.
        if avg > 0 and qty == self.shares and self.fill_qty <= 0:
            if abs(avg - self.avg_cost) >= 1:
                self._emit(f"평단 보정(계좌 기준): {self.avg_cost:,.0f} → {avg:,.0f}")
            self.avg_cost = avg
            self._avg_synced = True

    async def on_tick(
        self,
        tick: Tick,
        buy_fn: Callable[[int], Awaitable[float]],
        sell_fn: Callable[[int], Awaitable[float]],
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
                    fill = await buy_fn(leg.amount)
                    if fill > 0:
                        qty = int(leg.amount // fill)
                        if qty > 0:
                            new_shares = self.shares + qty
                            self.avg_cost = (self.avg_cost * self.shares + fill * qty) / new_shares
                            self.shares = new_shares
                            leg.filled = True
                            self._emit(f"{i+1}차 매수 {qty}주 @ {fill:,.0f} (평단 {self.avg_cost:,.0f})")
                            # 위 수량·평단은 직전 틱 가격 기준 추정치다.
                            # 익절·손절이 이 평단에 걸리므로 계좌로 보정한다.
                            self._avg_synced = False
                            await self._sync_from_account()
                    else:
                        leg.filled = True  # 체결 실패해도 무한루프 방지
                break  # 한 틱에 한 단계만
            if self.all_filled:
                self.phase = Phase.HOLDING
                self._emit("분할매수 완료 → HOLDING")

        if self.shares <= 0:
            return

        # 익절·손절은 모두 평단 기준이다. 주문 직후에는 계좌(kt00018)가 아직
        # 체결을 반영하지 못해 보정이 실패할 수 있으므로, 확인될 때까지 판단
        # *직전에* 재시도한다. 확인된 뒤에는 다시 조회하지 않는다(다음 매수까지
        # 평단이 변하지 않으므로). 이 보정 없이는 추정 평단으로 익절선을
        # 계산해 엉뚱한 가격에 청산된다.
        if not self._avg_synced:
            await self._sync_from_account()
            if self.shares <= 0:
                return

        # 손절 활성 조건: 마지막 분할 매수 체결 이후
        stop_active = self.all_filled

        # 2) 트레일링 모드
        if self.phase == Phase.TRAILING:
            self.trail_max = max(self.trail_max, price)
            # 손절 우선
            if price <= self.avg_cost * (1 - c.ulc_sl):
                await self._exit_all(sell_fn, "트레일링 중 손절")
                return
            # 상한가 도달
            if price >= self.x * 1.295:
                await self._exit_all(sell_fn, "상한가 도달")
                return
            # 보장 익절
            if price >= self.avg_cost * (1 + c.ulc_g):
                await self._exit_all(sell_fn, "보장 익절")
                return
            # 트레일링 스탑
            if price <= self.trail_max * (1 - c.ulc_t):
                await self._exit_all(sell_fn, f"트레일링 스탑 (max {self.trail_max:,.0f})")
                return
            return

        # 3) HOLDING: 익절/손절
        if self.phase == Phase.HOLDING:
            if stop_active and price <= self.avg_cost * (1 - c.ulc_sl):
                await self._exit_all(sell_fn, "손절")
                return
            if price >= self.avg_cost * (1 + c.ulc_tp):
                if c.ulc_trailing:
                    # 반익절 수량도 추정치가 아닌 실보유에서 계산한다.
                    await self._sync_from_account()
                    half = self.shares // 2
                    if half > 0:
                        fill = await sell_fn(half)
                        self.shares -= half
                        self.half_sold = True
                        self._emit(f"익절: 절반 {half}주 매도 @ {fill:,.0f} → 트레일링 가동")
                    self.trail_max = price
                    self.phase = Phase.TRAILING
                else:
                    await self._exit_all(sell_fn, "익절 전량")
                return

    async def _exit_all(self, sell_fn: Callable[[int], Awaitable[float]], reason: str) -> None:
        # 청산 수량은 추정치가 아니라 실보유를 따른다(과다 매도 → 주문거부 방지).
        await self._sync_from_account()
        if self.shares > 0:
            fill = await sell_fn(self.shares)
            self._emit(f"{reason}: 전량 {self.shares}주 매도 @ {fill:,.0f}")
            self.shares = 0
        self.phase = Phase.DONE
