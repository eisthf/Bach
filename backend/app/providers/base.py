"""데이터 제공자 / 브로커 추상 인터페이스.

mock 과 kiwoom(live) 구현이 동일한 계약을 따르게 한다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, Callable, List

from ..models import Bar, OrderResult, Tick


# 지원 분봉 간격(분)
VALID_INTERVALS = (3, 5, 10, 30, 60)


class DataProvider(ABC):
    """시세/봉 데이터 소스."""

    @abstractmethod
    def get_bars(self, code: str, interval: int, lookback_extra: int = 60) -> List[Bar]:
        """당일 봉 + 이전 ``lookback_extra``개 봉(SMA 계산용)을 시간순으로 반환.

        interval: 분 단위(3/5/10/30/60).
        """

    @abstractmethod
    async def stream_ticks(self, code: str) -> AsyncIterator[Tick]:
        """해당 종목의 실시간 틱을 비동기로 yield."""

    @abstractmethod
    def last_tick(self, code: str) -> Tick | None:
        """가장 최근 틱(없으면 None). 주문 체결가 산정 등에 사용."""

    # -- 자동매매 엔진 셋업용 X/Z (선택적; 없으면 봉에서 추정) --------------
    def prev_close(self, code: str) -> float | None:
        """전일 종가(X). 제공 못 하면 None → 호출측이 봉으로 추정."""
        return None

    def day_open(self, code: str) -> float | None:
        """당일 시가(Z). 제공 못 하면 None → 호출측이 봉/틱으로 추정."""
        return None

    def stock_name(self, code: str) -> str | None:
        """종목명. 제공 못 하면 None → 호출측이 코드로 대체."""
        return None


class Broker(ABC):
    """주문 실행 + 포지션 관리."""

    @abstractmethod
    def buy(self, code: str, amount_krw: int) -> OrderResult:
        """금액 기준 매수(현재가로 수량 환산)."""

    @abstractmethod
    def sell(self, code: str, qty: int) -> OrderResult:
        """수량 기준 매도."""

    @abstractmethod
    def position(self, code: str):
        """해당 종목 포지션(models.Position) 반환."""

    @abstractmethod
    def all_positions(self) -> list:
        """전체 포지션 목록."""

    def invalidate(self) -> None:
        """포지션 캐시 무효화(다음 조회 시 강제 갱신). 캐시 없으면 no-op."""

    def account_summary(self) -> dict | None:
        """계좌 요약(예수금/주문가능금액/평가금액 등). 제공 못 하면 None."""
        return None
