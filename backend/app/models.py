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
    ulc_manual_on_stop: bool = Field(
        False, description="분할매수 완료 후 손절선 도달 시 자동매도 없이 수동 인계")

    # 트레일링 스탑
    ulc_trailing: bool = Field(False, description="트레일링 스탑 활성화")
    ulc_t: float = Field(0.02, description="trail_max 대비 하락 청산 비율")
    ulc_g: float = Field(0.15, description="평단 대비 보장 익절 비율")

    # 실행 모드
    ulc_first_buy_only: bool = Field(False, description="1차 매수만 실행")
    use_3min_bar_timing: bool = Field(
        False, description="완성된 3분봉 종가가 확정될 때만 매매 조건 평가")


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
    position_verified: bool = True
    recovery_notice: str = ""


class MarketStatus(BaseModel):
    phase: MarketPhase
    auto: bool = False   # True=실시간 KST 시계 자동(live), False=수동 토글(mock)


# ---------------------------------------------------------------------------
# 상한가 스크리너
# ---------------------------------------------------------------------------
class UpperLimitStock(BaseModel):
    """D일 상한가(직전 거래일 종가 대비 +29~30%) 종목 한 건."""
    code: str
    name: str = ""
    market: str = ""                 # "KOSPI" | "KOSDAQ"
    close: int                       # D일 종가
    prev_close: int                  # 직전 거래일 종가
    change_pct: float                # 등락률(%)
    volume: int = 0                  # 조회일 거래량(키움 시세면 현재까지 누적)
    market_cap: int = 0              # 시가총액(원)


class UpperLimitResult(BaseModel):
    date: str                        # 조회일 D (YYYY-MM-DD)
    prev_date: str                   # 비교 기준이 된 직전 거래일 (YYYY-MM-DD)
    min_pct: float
    max_pct: float
    source: str                      # "krx"=확정 일별 | "kiwoom"=당일 | "mock"=데모
    snapshot: bool = False           # True면 확정 종가가 아닌 당일 시세 스냅샷
    scanned: int                     # D일 조회된 전체 종목 수
    stocks: list[UpperLimitStock] = []
