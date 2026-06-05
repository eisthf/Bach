"""장 운영 단계(market phase) 관리.

- **mock 모드**: 수동 토글(open/close/reset)로 장 이벤트를 발생시킨다(데모/테스트).
- **live(kiwoom) 모드**: 실제 KST 시계로 자동 판정한다. 정규장 09:00~15:30(평일)
  이며, phase는 시각으로 계산되고 09:00/15:30 경계에서 Hub가 MARKET-OPEN/CLOSE를
  자동 발생시킨다(수동 버튼 불필요).
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone

from .models import MarketPhase

# 한국 표준시(UTC+9, DST 없음) — tzdata 의존 없이 고정 오프셋 사용
KST = timezone(timedelta(hours=9))

REGULAR_OPEN = dtime(9, 0)
REGULAR_CLOSE = dtime(15, 30)


def now_kst() -> datetime:
    return datetime.now(KST)


def phase_for(dt: datetime) -> MarketPhase:
    """KST 시각 → 장 단계. 주말은 종일 CLOSED, 평일은 시각으로 판정."""
    if dt.weekday() >= 5:  # 토(5)/일(6)
        return MarketPhase.CLOSED
    t = dt.time()
    if t < REGULAR_OPEN:
        return MarketPhase.PRE_OPEN
    if t <= REGULAR_CLOSE:
        return MarketPhase.OPEN
    return MarketPhase.CLOSED


class MarketClock:
    def __init__(self, auto: bool = False) -> None:
        self.auto = auto
        self._phase = phase_for(now_kst()) if auto else MarketPhase.PRE_OPEN

    @property
    def phase(self) -> MarketPhase:
        # auto(live) 모드에선 항상 실제 KST 시각으로 계산한다.
        if self.auto:
            return phase_for(now_kst())
        return self._phase

    def is_pre_open(self) -> bool:
        return self.phase == MarketPhase.PRE_OPEN

    def is_open(self) -> bool:
        return self.phase == MarketPhase.OPEN

    # 수동 제어(mock 전용). auto 모드에선 무시(시각이 진실원).
    def open(self) -> None:
        if not self.auto:
            self._phase = MarketPhase.OPEN

    def close(self) -> None:
        if not self.auto:
            self._phase = MarketPhase.CLOSED

    def reset(self) -> None:
        if not self.auto:
            self._phase = MarketPhase.PRE_OPEN
