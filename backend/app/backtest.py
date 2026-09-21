"""ULC 자동매매 백테스트 — 과거 1분봉을 실제 ``UlcEngine`` 에 틱으로 재생한다.

전략 판단은 운영 코드(``strategy/ulc.py``)를 그대로 쓰므로 로직 차이가 없다.
근사는 가격 경로와 체결가뿐이다:

- 1분봉 하나를 틱 4개로 편다: 시가 → (저가·고가 또는 고가·저가) → 종가.
  봉 안의 고저 순서는 알 수 없으므로 ``order`` 로 가정을 고른다.
  ``auto`` 는 양봉이면 저가 먼저, 음봉이면 고가 먼저(흔한 경로)다.
- 시장가 주문은 그 틱 가격에 전량 체결된다고 본다(슬리피지 없음).
- ``ulc_first_buy_ask3`` 1차 매수는 현재가 + 2호가(매도 3호가 근사)에 체결로 본다.
- 장 마감까지 청산되지 않은 잔량은 당일 종가로 평가한다(실제로는 수동 인계).

X(전일 종가)와 Z(당일 시가)는 일봉에서 얻는다. 운영에서는 Z 를 검증된 정규장
체결 이벤트로 얻지만, 과거 날짜에는 그 이벤트가 없어 일봉 시가로 대신한다.
"""
from __future__ import annotations

import calendar
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .models import AutoConfig, Tick
from .pricing import tick_size
from .strategy.ulc import UlcEngine

ORDERS = ("auto", "high_first", "low_first")
COMMISSION = 0.00015  # 증권사 수수료(매수·매도 각각)
SELL_TAX = 0.0020     # 증권거래세(매도)
LAST_BAR = "1530"     # 정규장 종가 단일가 봉. 이후(시간외)는 재생하지 않는다.


@dataclass
class DayData:
    code: str
    date: str               # YYYYMMDD
    x: float                # 전일 종가
    z: float                # 당일 시가
    bars: List[dict]        # 당일 1분봉 {_hhmm, time, open, high, low, close, volume}

    @property
    def close(self) -> float:
        return self.bars[-1]["close"]


@dataclass
class Trade:
    hhmm: str
    side: str               # "buy" | "sell"
    qty: int
    price: float


@dataclass
class BacktestResult:
    day: DayData
    order: str
    trades: List[Trade] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)
    commission: float = COMMISSION
    sell_tax: float = SELL_TAX

    @property
    def cost(self) -> float:
        return sum(t.qty * t.price for t in self.trades if t.side == "buy")

    @property
    def proceeds(self) -> float:
        return sum(t.qty * t.price for t in self.trades if t.side == "sell")

    @property
    def held(self) -> int:
        bought = sum(t.qty for t in self.trades if t.side == "buy")
        return bought - sum(t.qty for t in self.trades if t.side == "sell")

    @property
    def mark(self) -> float:
        return self.held * self.day.close

    @property
    def pnl(self) -> float:
        return self.proceeds + self.mark - self.cost

    @property
    def fees(self) -> float:
        # 미청산 잔량도 종가에 판 것으로 보고 매도 비용을 반영한다.
        return self.cost * self.commission + (self.proceeds + self.mark) * (self.commission + self.sell_tax)

    @property
    def net_pnl(self) -> float:
        return self.pnl - self.fees

    @property
    def return_pct(self) -> float:
        return self.net_pnl / self.cost * 100 if self.cost else 0.0


def bar_prices(bar: dict, order: str = "auto") -> List[float]:
    """1분봉을 틱 가격 4개로 편다."""
    if order not in ORDERS:
        raise ValueError(f"order must be one of {ORDERS}")
    low_first = bar["close"] >= bar["open"] if order == "auto" else order == "low_first"
    middle = [bar["low"], bar["high"]] if low_first else [bar["high"], bar["low"]]
    return [bar["open"], *middle, bar["close"]]


async def replay(day: DayData, config: AutoConfig, order: str = "auto",
                 commission: float = COMMISSION, sell_tax: float = SELL_TAX) -> BacktestResult:
    result = BacktestResult(day=day, order=order, commission=commission, sell_tax=sell_tax)
    engine = UlcEngine(code=day.code, config=config, x=day.x, z=day.z, log=result.logs.append)
    engine.setup()
    now = {"hhmm": "", "price": 0.0}

    async def buy(amount: int) -> float:
        price = now["price"]
        if config.ulc_first_buy_ask3 and engine.shares <= 0:
            price += 2 * tick_size(price)
        qty = int(amount // price)
        if qty > 0:
            result.trades.append(Trade(now["hhmm"], "buy", qty, price))
        return price

    async def sell(qty: int) -> float:
        result.trades.append(Trade(now["hhmm"], "sell", qty, now["price"]))
        return now["price"]

    for bar in day.bars:
        if bar["_hhmm"] > LAST_BAR:
            break
        # 틱 시각은 봉 안에서 15초 간격으로 벌려 3분봉 버킷 판정이 봉 시각을 따르게 한다.
        for i, price in enumerate(bar_prices(bar, order)):
            now.update(hhmm=bar["_hhmm"], price=price)
            tick = Tick(code=day.code, price=price, high=price, low=price, open=day.z,
                        open_verified=True, time=bar["time"] + i * 15)
            await engine.on_tick(tick, buy, sell)
    return result


# ---------------------------------------------------------------------------
# 데이터 적재 (키움 조회 + 로컬 캐시)
# ---------------------------------------------------------------------------
def day_from_rows(code: str, date: str, daily: List[dict], minutes: List[dict]) -> DayData:
    """일봉(오름차순)·분봉 행에서 X/Z 를 뽑아 DayData 를 만든다."""
    index = next((i for i, row in enumerate(daily) if row["_date"] == date), None)
    if index is None:
        raise ValueError(f"[{code}] {date} 일봉이 없습니다(휴장일이거나 거래정지).")
    if index == 0:
        raise ValueError(f"[{code}] {date} 직전 거래일 일봉이 없습니다.")
    if not minutes:
        raise ValueError(f"[{code}] {date} 분봉이 없습니다.")
    return DayData(code=code, date=date, x=daily[index - 1]["close"],
                   z=daily[index]["open"], bars=minutes)


def load_day(code: str, date: str, *, token_fn, mock: bool,
             cache_dir: Optional[Path] = None, refresh: bool = False) -> DayData:
    """키움에서 하루치 데이터를 받아온다. 캐시가 있으면 네트워크를 쓰지 않는다.

    ``token_fn`` 은 필요할 때만 호출된다(전부 캐시 적중이면 토큰을 발급하지 않는다).
    """
    from .providers import kiwoom_api as kw

    path = cache_dir / f"{code}_{date}.json" if cache_dir else None
    if path and path.exists() and not refresh:
        raw = json.loads(path.read_text(encoding="utf-8"))
    else:
        token = token_fn()
        # 오늘 이후를 기준일로 주면 안 되므로 조회 기준일은 대상일 그 자체다.
        daily = kw.fetch_day_bars(token, code, mock=mock, today=date, lookback_extra=5)
        minutes = kw.fetch_min_bars_on(token, code, date, mock=mock)
        raw = {"daily": daily, "minutes": minutes}
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return day_from_rows(code, date, raw["daily"], raw["minutes"])


def parse_case(text: str) -> tuple[str, str]:
    """'079650:20260903' 또는 '079650:2026-09-03' → (code, YYYYMMDD)."""
    code, _, date = text.partition(":")
    date = date.replace("-", "")
    if len(code) != 6 or not code.isdigit():
        raise ValueError(f"종목코드 형식 오류: {text!r} (예: 079650:20260903)")
    datetime.strptime(date, "%Y%m%d")
    return code, date


def minute_epoch(date: str, hhmm: str) -> int:
    """테스트·합성 데이터용: KST 벽시계를 UTC 로 간주한 epoch(차트 시간축과 동일)."""
    return calendar.timegm(datetime.strptime(date + hhmm, "%Y%m%d%H%M").timetuple())
