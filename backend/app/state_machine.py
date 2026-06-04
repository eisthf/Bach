"""종목별 상태머신.

요구 다이어그램(project_requirement.md):

    [*] --> MANUAL_TRADING
    MANUAL_TRADING --> MONITOR : PUSH [장전]
    MONITOR --> MANUAL_TRADING : PUSH
    MONITOR --> AUTO_TRADING : MARKET-OPEN
    AUTO_TRADING --> MANUAL_TRADING : PUSH
    AUTO_TRADING --> MANUAL_TRADING : LIQUIDATE
    (장중 MANUAL_TRADING은 종착 — 나가는 전이 없음)

이벤트:
- PUSH      : 버튼 누름
- MARKET_OPEN / MARKET_CLOSE : 장 이벤트
- LIQUIDATE : 전량청산 후 수동 복귀
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

    def liquidate(self) -> TradeState:
        """AUTO_TRADING 전량청산 후 MANUAL_TRADING으로."""
        if self.state == TradeState.AUTO_TRADING:
            self.state = TradeState.MANUAL_TRADING
        return self.state

    def on_market_open(self) -> TradeState:
        """장 시작. MONITOR 종목만 AUTO_TRADING으로 진입."""
        if self.state == TradeState.MONITOR:
            self.state = TradeState.AUTO_TRADING
        return self.state

    def on_market_close(self) -> TradeState:
        """장 종료. 본 다이어그램에서 별도 강제 전이는 없음."""
        return self.state

    def can_push(self) -> bool:
        """현재 PUSH가 의미 있는 전이를 일으키는가(프런트 버튼 활성화용)."""
        s = self.state
        if s == TradeState.MANUAL_TRADING:
            return self._clock.is_pre_open()
        return True  # MONITOR, AUTO_TRADING 에선 항상 전이 가능
