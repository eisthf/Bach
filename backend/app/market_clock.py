"""장 운영 단계(market phase) 관리.

mock 모드에서는 수동 토글(open/close)로 장 이벤트를 발생시킨다.
live 모드에서는 실제 시계(09:00~15:30 KST)로 자동 판정하도록 확장 가능.
"""
from __future__ import annotations

from .models import MarketPhase


class MarketClock:
    def __init__(self) -> None:
        self._phase = MarketPhase.PRE_OPEN

    @property
    def phase(self) -> MarketPhase:
        return self._phase

    def is_pre_open(self) -> bool:
        return self._phase == MarketPhase.PRE_OPEN

    def is_open(self) -> bool:
        return self._phase == MarketPhase.OPEN

    def open(self) -> None:
        self._phase = MarketPhase.OPEN

    def close(self) -> None:
        self._phase = MarketPhase.CLOSED

    def reset(self) -> None:
        self._phase = MarketPhase.PRE_OPEN
