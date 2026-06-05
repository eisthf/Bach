"""종목별 상태머신.

요구 다이어그램(project_requirement.md):

    [*] --> MANUAL_TRADING
    MANUAL_TRADING --> MONITOR : PUSH [장전]
    MONITOR --> MANUAL_TRADING : PUSH
    MONITOR --> AUTO_TRADING : MARKET-OPEN
    AUTO_TRADING --> MANUAL_TRADING : PUSH
    AUTO_TRADING --> MANUAL_TRADING : POSITION-FLAT(보유수량→0)
    (any) --> MANUAL_TRADING : MARKET-CLOSE
    (장중 MANUAL_TRADING은 종착 — 나가는 전이 없음)

청산(LIQUIDATE)은 버튼이 아니라 *매매의 결과*다. 자동매매 엔진이 전량
매도하여 보유수량이 (>0에서) 0이 되는 순간 POSITION-FLAT 이벤트가 발생하고,
이때 AUTO_TRADING → MANUAL_TRADING으로 전이한다. 사람이 직접 청산하려면
PUSH로 수동 인수한 뒤 수동매매에서 매도한다(그 경우엔 이미 MANUAL이라 전이 없음).

장 종료(MARKET-CLOSE)는 하루 거래 사이클의 끝이다. 모든 종목을
MANUAL_TRADING으로 되돌리고, Hub가 장 단계를 PRE_OPEN(다음 거래일 장전)으로
리셋하여 다시 MONITOR 진입이 가능한 초기 상태로 복귀시킨다.

이벤트:
- PUSH          : 버튼 누름
- MARKET_OPEN / MARKET_CLOSE : 장 이벤트
- POSITION_FLAT : 보유수량 0 도달(엔진 청산 결과) → 수동 복귀
"""
from __future__ import annotations

from .market_clock import MarketClock
from .models import TradeState


class StateMachine:
    def __init__(self, clock: MarketClock, initial: TradeState = TradeState.MANUAL_TRADING) -> None:
        self._clock = clock
        self.state = initial

    # -- 이벤트 핸들러 -----------------------------------------------------
    def push(self) -> TradeState:
        """버튼 PUSH. 전이 규칙은 현재 state + 장 단계에 의존."""
        s = self.state
        if s == TradeState.MANUAL_TRADING:
            # 장전에만 MONITOR로. 장중 MANUAL_TRADING은 종착(무시).
            if self._clock.is_pre_open():
                self.state = TradeState.MONITOR
        elif s == TradeState.MONITOR:
            self.state = TradeState.MANUAL_TRADING
        elif s == TradeState.AUTO_TRADING:
            # 수동 전환. 한 번 MANUAL로 가면 장중엔 돌아오지 않음(종착).
            self.state = TradeState.MANUAL_TRADING
        return self.state

    def on_position_flat(self) -> TradeState:
        """보유수량이 0이 됨(POSITION-FLAT). 자동매매 청산 완료 → MANUAL_TRADING.

        버튼 이벤트가 아니라, 엔진의 전량매도로 포지션이 비워진 *결과*로
        호출된다. AUTO_TRADING일 때만 의미 있는 전이를 일으킨다.
        """
        if self.state == TradeState.AUTO_TRADING:
            self.state = TradeState.MANUAL_TRADING
        return self.state

    def on_market_open(self) -> TradeState:
        """장 시작. MONITOR 종목만 AUTO_TRADING으로 진입."""
        if self.state == TradeState.MONITOR:
            self.state = TradeState.AUTO_TRADING
        return self.state

    def on_market_close(self) -> TradeState:
        """장 종료. 하루 사이클 종료 → MANUAL_TRADING 초기 상태로 복귀.

        장 단계의 PRE_OPEN 리셋은 Hub가 담당한다(state_machine은 clock을
        직접 바꾸지 않음). 둘이 합쳐져 '장전 + 수동매매' 초기 상태가 된다.
        """
        self.state = TradeState.MANUAL_TRADING
        return self.state

    def can_push(self) -> bool:
        """현재 PUSH가 의미 있는 전이를 일으키는가(프런트 버튼 활성화용)."""
        s = self.state
        if s == TradeState.MANUAL_TRADING:
            return self._clock.is_pre_open()
        return True  # MONITOR, AUTO_TRADING 에선 항상 전이 가능
