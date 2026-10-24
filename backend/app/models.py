"""Pydantic 모델 및 도메인 enum.

프런트엔드와 주고받는 모든 페이로드의 단일 정의처.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# 상태머신
# ---------------------------------------------------------------------------
class TradeState(str, Enum):
    # 대기: 장전 설정·PUSH 대기, 또는 그날 자동매매를 마친 종목(장중엔 다시 감시 불가).
    # 사람이 보유를 관리하는 수동 매매는 별도 목록(manual_trade.py)이다. 예전 저장값
    # "MANUAL_TRADING"은 복원 때 IDLE로 읽는다.
    IDLE = "IDLE"
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
    # 거래대금(원). 일봉만 채운다(키움 ka10081 trde_prica). 분봉은 원천에 없어 0.
    amount: float = 0.0


class Tick(BaseModel):
    code: str
    price: float           # 현재가
    high: float            # 장중 고가
    low: float             # 장중 저가
    open: float            # 시가
    received_ns: int = Field(0, exclude=True)  # 프로세스 내부 지연 계측 전용
    open_verified: bool = False  # KRX 정규장 체결 메시지로 검증된 시가
    volume: float = 0.0
    amount: float = 0.0    # 당일 누적 거래대금(원). 0 = 원천 미제공
    time: int = 0          # epoch seconds


# ---------------------------------------------------------------------------
# 자동매매 설정 (upper_limit_chase 파라미터)
# ---------------------------------------------------------------------------
class AutoConfig(BaseModel):
    """`trading_config.yaml`의 종목별 설정 구조를 차용.

    상한가 따라잡기(upper_limit_chase) 전략 파라미터.
    """
    max_buy_amount: int = Field(1_000_000, description="총 투자액(원), 분할매수로 배분")

    # 시나리오 경계
    ulc_p: float = Field(0.05, description="SC1/SC2 경계 갭 비율")
    ulc_p1: float = Field(0.07, description="SC2/SC3 경계 갭 비율")
    ulc_q: float = Field(0.05, description="SC1 2차 매수가 산정: X*(1-q)")

    # 진입 필터
    ulc_w: float = Field(0.15, description="갭 상한(초과 시 SKIP)")
    ulc_allow_lower_open: bool = Field(True, description="하락 시가도 SC1 진입 허용")

    # 손익 청산
    ulc_tp: float = Field(0.05, description="익절 비율(평단 대비)")
    ulc_sl: float = Field(0.05, description="손절 비율(평단 대비)")
    ulc_manual_on_stop: bool = Field(
        False, description="분할매수 완료 후 손절선 도달 시 자동매도 없이 수동 인계")
    ulc_stop_requires_below_prev_close: bool = Field(
        False, description="손절 조건 강화: 현재가 ≤ 손절선 이고 동시에 현재가 < 전일 종가(X)일 때만 손절")

    # 트레일링 스탑
    ulc_trailing: bool = Field(True, description="트레일링 스탑 활성화")
    ulc_t: float = Field(0.02, description="trail_max 대비 하락 청산 비율")
    ulc_g: float = Field(0.15, description="평단 대비 보장 익절 비율")

    # 실행 모드
    ulc_first_buy_only: bool = Field(False, description="1차 매수만 실행")
    ulc_first_buy_ask3: bool = Field(True, description="1차 매수를 매도 3호가 지정가로 주문")
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
    volume: int = 0                  # 조회일 거래량(키움 시세면 KRX+NXT 통합 누적)
    market_cap: int = 0              # 시가총액(원)
    amount: int = 0                  # 조회일 거래대금(원). 0 = 모름(예전 저장본 등)
    regular_close: int = 0           # 정규장(15:30) 종가. 0 = 모름/아직 정규장 중
    after_hours: bool = False        # 정규장 종가는 상한가 미만이고 시간외(KRX·NXT)에서 도달
    # 정규장은 상한가로 마감했지만 시간외에서 밀려 마지막 가격이 상한가 구간 밖.
    # close/change_pct 는 마지막 거래가 기준(모르면 정규장 종가), regular_close 는 정규장 종가.
    after_hours_drop: bool = False


class UpperLimitResult(BaseModel):
    date: str                        # 조회일 D (YYYY-MM-DD)
    prev_date: str                   # 비교 기준이 된 직전 거래일 (YYYY-MM-DD)
    min_pct: float
    max_pct: float
    source: str                      # "krx"=확정 일별 | "kiwoom"=당일 | "mock"=데모
    snapshot: bool = False           # True면 확정 종가가 아닌 당일 시세 스냅샷
    captured_at: str = ""            # 키움 조회 시각 (KST ISO)
    cached: bool = False             # 저장해 둔 과거 키움 조회 결과
    notice: str = ""                 # 최신 자료 게시 전 이전 결과 표시 안내
    scanned: int                     # D일 조회된 전체 종목 수
    stocks: list[UpperLimitStock] = []


class CandleTradeConfig(BaseModel):
    """대금 양봉 매매 설정.

    매입 구간 = 기준 캔들 몸통(시가~종가)의 양 끝에서 각각 몸통 길이의
    ``zone_margin_pct``% 만큼 안쪽으로 들어온 가격 범위.
    """
    total_krw: int = Field(1_000_000, ge=10_000, description="총 투자액(원)")
    first_ratio_pct: float = Field(50.0, ge=10, le=90, description="1차 매수 비중(%). 나머지가 2차")
    zone_margin_pct: float = Field(10.0, ge=0, lt=50, description="매입 구간: 몸통 양 끝에서 안쪽으로(몸통 대비 %)")
    # 2차 매수 조건가 (P=1차 매수가, L·H=구간 하단·상단):
    #   fixed = P×(1−u%)      gap = H − k%×(H−L), 단 1차가 대비 하락폭이 최소 하락률 미만이면 2차 없음
    #   mixed = (P−L)/P > u% 일 때만 min(fixed, H − k%×(H−L)) — 둘 중 더 깊은 하락. 아니면 2차 없음.
    #           구간이 넓을 때 −u% 고정은 너무 위에서 사게 되므로 하단 쪽에서 사되, 최소 u%는 내려간다.
    add_mode: Literal["fixed", "gap", "mixed"] = Field("mixed", description="2차 매수 기준")
    add_drop_pct: float = Field(5.0, gt=0, lt=100, description="2차 매수: 고정=1차가 대비 하락률, 혼합=1차가~하단 여유 문턱(%) u")
    add_gap_pct: float = Field(80.0, gt=0, le=100, description="2차 매수: 구간 상단에서 구간 폭의 k% 아래(%) k")
    add_min_drop_pct: float = Field(2.0, ge=0, lt=100,
                                    description="갭 기준 2차의 최소 하락률(%). 갭이 작아 이보다 얕으면 2차 없음")
    take_profit_pct: float = Field(20.0, gt=0, le=500, description="익절: 평단 대비 상승률(%)")
    # 익절 방식. True=절반 익절 후 나머지 트레일링(기본), False=익절가에서 전량 매도로 끝.
    trailing: bool = Field(True, description="절반 익절 후 나머지 트레일링 스톱. 끄면 익절가에서 전량 매도")
    trailing_pct: float = Field(5.0, gt=0, lt=100, description="나머지 트레일링 스톱: 고점 대비 하락률(%)")
    entry_days: int = Field(5, ge=1, le=60, description="1차 매수 기한: 기준일 이후 거래일 수")
    watch_mode: Literal["tick", "minute"] = Field("tick", description="가격 감시 단위")
    watch_minutes: int = Field(3, description="분 단위 감시의 봉 길이(분)")

    @model_validator(mode="after")
    def _check(self) -> "CandleTradeConfig":
        if self.watch_minutes not in WATCH_MINUTES:
            raise ValueError(f"분 단위 감시는 {', '.join(map(str, WATCH_MINUTES))}분 중 하나여야 합니다.")
        return self


WATCH_MINUTES = (1, 3, 5, 10, 15, 30, 60)


class BigCandleStock(BaseModel):
    """거래대금 기준 이상이면서 양봉(현재가/종가 > 시가)인 종목 한 건."""
    code: str
    name: str = ""
    market: str = ""                 # "KOSPI" | "KOSDAQ" (모르면 빈 값)
    open: int                        # 당일 시가
    close: int                       # 장중=현재가, 장 이후=종가
    high: int = 0                    # 당일 고가. 0 = 모름
    rise_pct: float                  # 시가 대비 상승률(%)
    change_pct: Optional[float] = None  # 전일 종가 대비 등락률(%). 모르면 None
    volume: int = 0
    amount: int = 0                  # 거래대금(원)
    market_cap: int = 0              # 시가총액(원). 모르면 0
    etp: bool = False                # ETF·ETN 여부(KRX 일별 주식 자료에는 애초에 없다)


class BigCandleResult(BaseModel):
    date: str                        # 조회일 (YYYY-MM-DD)
    min_rise_pct: float              # 시가 대비 최소 상승률(%)
    min_change_pct: float = 0.0      # 전일 대비 최소 등락률(%). 음수 가능
    min_amount: int                  # 최소 거래대금(원)
    source: str                      # "kiwoom"=당일 | "krx"=확정 일별 | "mock"=데모
    snapshot: bool = False           # True면 키움 당일 시세 스냅샷
    closed: bool = True              # 조회 시점에 정규장이 끝났는가(종가 기준인가)
    etp_known: bool = True           # 종목별 etp 구분이 유효한가(ETF·ETN 목록 조회 실패 시 False)
    captured_at: str = ""            # 키움 조회 시각 (KST ISO)
    notice: str = ""
    scanned: int                     # 거래대금 기준을 넘은 종목 수(양봉 여부 무관)
    stocks: list[BigCandleStock] = []
