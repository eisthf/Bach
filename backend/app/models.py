"""Pydantic 모델 및 도메인 enum.

프런트엔드와 주고받는 모든 페이로드의 단일 정의처.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 상태머신
# ---------------------------------------------------------------------------
class TradeState(str, Enum):
    MANUAL_TRADING = "MANUAL_TRADING"
    MONITOR = "MONITOR"
    AUTO_TRADING = "AUTO_TRADING"


class MarketPhase(str, Enum):
    """장 운영 단계. 상태 전이 가드에 사용."""
    PRE_OPEN = "PRE_OPEN"   # 장전
    OPEN = "OPEN"           # 장중
    CLOSED = "CLOSED"       # 장종료


# ---------------------------------------------------------------------------
# 시세 / 차트
# ---------------------------------------------------------------------------
# lightweight-charts는 UTCTimestamp(초 단위)를 시간축으로 사용한다.
class Bar(BaseModel):
    time: int = Field(..., description="봉 시작 시각 (epoch seconds, UTC)")
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class Tick(BaseModel):
    code: str
    price: float           # 현재가
    high: float            # 장중 고가
    low: float             # 장중 저가
    open: float            # 시가
    volume: float = 0.0
    time: int = 0          # epoch seconds


# ---------------------------------------------------------------------------
# 자동매매 설정 (upper_limit_chase 파라미터)
# ---------------------------------------------------------------------------
class AutoConfig(BaseModel):
    """`trading_config.yaml`의 종목별 설정 구조를 차용.

    상한가 따라잡기(upper_limit_chase) 전략 파라미터.
    """
    max_buy_amount: int = Field(500_000, description="총 투자액(원), 분할매수로 배분")

    # 시나리오 경계
    ulc_p: float = Field(0.05, description="SC1/SC2 경계 갭 비율")
    ulc_p1: float = Field(0.07, description="SC2/SC3 경계 갭 비율")
    ulc_q: float = Field(0.05, description="SC1 2차 매수가 산정: X*(1-q)")

    # 진입 필터
    ulc_w: float = Field(0.15, description="갭 상한(초과 시 SKIP)")
    ulc_allow_lower_open: bool = Field(False, description="하락 시가도 SC1 진입 허용")

    # 손익 청산
    ulc_tp: float = Field(0.05, description="익절 비율(평단 대비)")
    ulc_sl: float = Field(0.05, description="손절 비율(평단 대비)")

    # 트레일링 스탑
    ulc_trailing: bool = Field(False, description="트레일링 스탑 활성화")
    ulc_t: float = Field(0.02, description="trail_max 대비 하락 청산 비율")
    ulc_g: float = Field(0.15, description="평단 대비 보장 익절 비율")

    # 실행 모드
    ulc_first_buy_only: bool = Field(False, description="1차 매수만 실행")
    use_3min_bar_timing: bool = Field(False, description="3분봉 종가 기준 매매")


# ---------------------------------------------------------------------------
# 주문 / 포지션
# ---------------------------------------------------------------------------
class BuyOrderReq(BaseModel):
    code: str
    amount_krw: int = Field(..., gt=0, description="매수 금액(원)")


class SellOrderReq(BaseModel):
    code: str
    qty: int = Field(..., gt=0, description="매도 수량(주)")


class OrderResult(BaseModel):
    ok: bool
    code: str
    side: str            # "buy" | "sell"
    filled_qty: int
    price: float
    order_no: Optional[str] = None
    message: str = ""


class Position(BaseModel):
    code: str
    quantity: int = 0
    avg_price: float = 0.0
    current_price: float = 0.0
    realized_pnl: float = 0.0

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.avg_price) * self.quantity


# ---------------------------------------------------------------------------
# 종목 / 상태 응답
# ---------------------------------------------------------------------------
class StockStatus(BaseModel):
    code: str
    name: str = ""
    state: TradeState
    config: AutoConfig
    position: Position


class MarketStatus(BaseModel):
    phase: MarketPhase
    auto: bool = False   # True=실시간 KST 시계 자동(live), False=수동 토글(mock)
