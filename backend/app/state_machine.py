"""종목별 상태머신 ([상한가 매매] 목록).

    [*] --> IDLE
    IDLE --> MONITOR : PUSH [장중이 아닐 때]
    MONITOR --> IDLE : PUSH
    MONITOR --> AUTO_TRADING : MARKET-OPEN
    AUTO_TRADING --> IDLE : POSITION-FLAT(보유수량→0)
    (any) --> IDLE : MARKET-CLOSE
    (장중 IDLE은 종착 — 나가는 전이 없음)

IDLE(대기)은 장전 설정·PUSH 대기이자, 그날 자동매매를 마친 종목의 자리다. 장중에 한 번
IDLE이 되면 그날은 다시 감시·자동으로 가지 않는다.

사람이 보유를 넘겨받는 수동 매매는 이 상태머신 밖이다 — [수동 전환](AUTO_TRADING에서의
PUSH 포함)은 Hub가 종목을 [상한가 매매] 목록에서 빼 수동 매매 목록(manual_trade.py)으로 옮긴다.

청산(LIQUIDATE)은 버튼이 아니라 *매매의 결과*다. 자동매매 엔진이 전량
매도하여 보유수량이 (>0에서) 0이 되는 순간 POSITION-FLAT 이벤트가 발생하고,
이때 AUTO_TRADING → IDLE로 전이한다(잔량이 남았으면 Hub가 수동 매매로 옮긴다).

장 종료(MARKET-CLOSE)는 하루 거래 사이클의 끝이다. 모든 종목을
IDLE로 되돌리고, Hub가 장 단계를 PRE_OPEN(다음 거래일 장전)으로
리셋하여 다시 MONITOR 진입이 가능한 초기 상태로 복귀시킨다.
예외: 보유 중인 AUTO_TRADING은 Hub가 이 전이를 부르지 않고 다음 거래일로
이월한다(Hub.apply_market_close).

이벤트:
- PUSH          : 버튼 누름
- MARKET_OPEN / MARKET_CLOSE : 장 이벤트
- POSITION_FLAT : 보유수량 0 도달(엔진 청산 결과) → 대기 복귀
"""
from __future__ import annotations

from .market_clock import MarketClock
from .models import TradeState


class StateMachine:
    def __init__(self, clock: MarketClock, initial: TradeState = TradeState.IDLE) -> None:
        self._clock = clock
        self.state = initial

    # -- 이벤트 핸들러 -----------------------------------------------------
    def push(self) -> TradeState:
        """버튼 PUSH. 전이 규칙은 현재 state + 장 단계에 의존.

        AUTO_TRADING의 PUSH는 수동 전환이라 Hub가 처리한다(목록 이동). 여기서는 IDLE로 둔다.
        """
        s = self.state
        if s == TradeState.IDLE:
            # 장전뿐 아니라 장 종료 후·주말에도 다음 장 감시를 미리 예약할 수
            # 있다. 실제 장중의 IDLE만 종착 상태로 둔다.
            if not self._clock.is_open():
                self.state = TradeState.MONITOR
        elif s in (TradeState.MONITOR, TradeState.AUTO_TRADING):
            self.state = TradeState.IDLE
        return self.state

    def on_position_flat(self) -> TradeState:
        """보유수량이 0이 됨(POSITION-FLAT). 자동매매 청산 완료 → IDLE.

        버튼 이벤트가 아니라, 엔진의 전량매도로 포지션이 비워진 *결과*로
        호출된다. AUTO_TRADING일 때만 의미 있는 전이를 일으킨다.
        """
        if self.state == TradeState.AUTO_TRADING:
            self.state = TradeState.IDLE
        return self.state

    def on_market_open(self) -> TradeState:
        """장 시작. MONITOR 종목만 AUTO_TRADING으로 진입."""
        if self.state == TradeState.MONITOR:
            self.state = TradeState.AUTO_TRADING
        return self.state

    def on_market_close(self) -> TradeState:
        """장 종료. 하루 사이클 종료 → IDLE 초기 상태로 복귀.

        장 단계의 PRE_OPEN 리셋은 Hub가 담당한다(state_machine은 clock을
        직접 바꾸지 않음). 둘이 합쳐져 '장전 + 대기' 초기 상태가 된다.
        """
        self.state = TradeState.IDLE
        return self.state

    def can_push(self) -> bool:
        """현재 PUSH가 의미 있는 전이를 일으키는가(프런트 버튼 활성화용)."""
        s = self.state
        if s == TradeState.IDLE:
            return not self._clock.is_open()
        return True  # MONITOR, AUTO_TRADING 에선 항상 전이 가능
