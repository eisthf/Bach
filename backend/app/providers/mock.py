"""Mock 데이터 제공자 + 브로커.

자격증명 없이 UI/상태머신/차트/전략을 완전히 동작시키기 위한 시뮬레이터.

- 봉 생성: 종목코드 시드 기반 랜덤워크로 '이전 60봉 + 당일 봉'을 만든다.
  상한가 따라잡기 흐름이 보이도록 전일 종가(=상한가 X) 부근에서 갭 상승한
  당일 시가(Z)를 만들어 ULC 시나리오가 자연히 발생하게 한다.
- 틱: 마지막 봉의 종가에서 출발하는 랜덤워크. 장중 고가/저가를 누적한다.
- 브로커: 현재가로 즉시 체결되는 시뮬 주문.
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import AsyncIterator, Dict, List

from ..models import Bar, OrderResult, Position, Tick
from .base import Broker, DataProvider

# KRX 호가 단위
def tick_size(price: float) -> int:
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def round_to_tick(price: float) -> float:
    t = tick_size(price)
    return round(price / t) * t


def _seed_for(code: str) -> int:
    return abs(hash(("bach-mock", code))) % (2**31)


# 당일 정규장 분 수(09:00~15:30 = 390분)
SESSION_MINUTES = 390
# 당일 봉의 기준 날짜를 위한 09:00 KST. UTC로는 00:00. 차트 표시용이라 정확한
# 타임존보다 '연속된 시간축'이 중요. epoch seconds 기준 임의 기준일 사용.
_BASE_DAY_EPOCH = 1_700_000_000  # 고정 기준(2023-11-14 부근), 표시 일관성용


class MockDataProvider(DataProvider):
    def __init__(self) -> None:
        # code -> 생성된 base 가격 정보 캐시(틱 시뮬과 봉이 일관되도록)
        self._open_price: Dict[str, float] = {}
        self._prev_close: Dict[str, float] = {}     # 전일 종가 = 상한가 X
        self._last_tick: Dict[str, Tick] = {}
        self._session_high: Dict[str, float] = {}
        self._session_low: Dict[str, float] = {}

    # -- 봉 생성 -----------------------------------------------------------
    def _ensure_base(self, code: str) -> None:
        if code in self._prev_close:
            return
        rng = random.Random(_seed_for(code))
        # 전일 종가(상한가) X: 1,000 ~ 60,000 사이
        x = round_to_tick(rng.uniform(3_000, 60_000))
        # 당일 시가 Z: ULC 시나리오가 골고루 나오도록 X 대비 0~12% 갭
        gap = rng.uniform(0.0, 0.12)
        z = round_to_tick(x * (1 + gap))
        self._prev_close[code] = x
        self._open_price[code] = z

    def get_bars(self, code: str, interval: int, lookback_extra: int = 60) -> List[Bar]:
        self._ensure_base(code)
        rng = random.Random(_seed_for(code) ^ (interval * 2654435761 & 0xFFFFFFFF))

        x = self._prev_close[code]
        z = self._open_price[code]

        bars_per_day = max(1, SESSION_MINUTES // interval)
        # 당일 첫 봉의 SMA60을 위해 이전 lookback_extra개 봉을 앞에 붙인다.
        total = lookback_extra + bars_per_day

        # 이전 구간은 X(전일 종가) 근처에서 수렴하도록 역방향 워크.
        prices: List[float] = []
        price = x
        for _ in range(lookback_extra):
            price = round_to_tick(price * (1 + rng.uniform(-0.012, 0.012)))
            price = max(price, tick_size(price))
            prices.append(price)
        prices.reverse()  # 시간순(과거->현재 직전)

        # 당일은 시가 Z에서 출발.
        day_prices: List[float] = []
        price = z
        for _ in range(bars_per_day):
            price = round_to_tick(price * (1 + rng.uniform(-0.02, 0.02)))
            price = max(price, tick_size(price))
            day_prices.append(price)

        closes = prices + day_prices

        bars: List[Bar] = []
        interval_sec = interval * 60
        # 이전 봉들은 당일 09:00 이전 시간축에 배치(연속된 음수 오프셋).
        start_epoch = _BASE_DAY_EPOCH - lookback_extra * interval_sec
        prev_close = closes[0]
        for i, close in enumerate(closes):
            o = prev_close
            # intrabar 변동
            hi = max(o, close) * (1 + abs(rng.uniform(0, 0.01)))
            lo = min(o, close) * (1 - abs(rng.uniform(0, 0.01)))
            hi = round_to_tick(hi)
            lo = round_to_tick(max(lo, tick_size(lo)))
            vol = rng.randint(1_000, 100_000)
            bars.append(
                Bar(
                    time=start_epoch + i * interval_sec,
                    open=round_to_tick(o),
                    high=hi,
                    low=lo,
                    close=close,
                    volume=vol,
                )
            )
            prev_close = close

        # 틱 시뮬은 마지막 봉 종가에서 이어진다(차트와 시각적 연속성).
        # 단, open 필드에는 당일 시가 Z를 보존(ULC 엔진이 시나리오 판정에 사용).
        last_close = closes[-1]
        now = int(time.time())
        self._last_tick[code] = Tick(
            code=code, price=last_close, high=last_close, low=last_close,
            open=z, volume=0, time=now,
        )
        self._session_high[code] = last_close
        self._session_low[code] = last_close
        return bars

    # -- X/Z (자동매매 엔진 셋업용) ---------------------------------------
    def prev_close(self, code: str) -> float | None:
        self._ensure_base(code)
        return self._prev_close.get(code)

    def day_open(self, code: str) -> float | None:
        self._ensure_base(code)
        return self._open_price.get(code)

    # -- 틱 시뮬 -----------------------------------------------------------
    def last_tick(self, code: str) -> Tick | None:
        return self._last_tick.get(code)

    async def stream_ticks(self, code: str) -> AsyncIterator[Tick]:
        self._ensure_base(code)
        if code not in self._last_tick:
            # 봉을 먼저 만들지 않았다면 시가 기준으로 초기화
            z = self._open_price[code]
            now = int(time.time())
            self._last_tick[code] = Tick(code=code, price=z, high=z, low=z, open=z, time=now)
            self._session_high[code] = z
            self._session_low[code] = z

        rng = random.Random(_seed_for(code) ^ 0xABCDEF)
        while True:
            await asyncio.sleep(1.0)
            prev = self._last_tick[code]
            step = prev.price * rng.uniform(-0.004, 0.0045)  # 약한 상방 바이어스
            price = round_to_tick(max(prev.price + step, tick_size(prev.price)))
            hi = max(self._session_high[code], price)
            lo = min(self._session_low[code], price)
            self._session_high[code] = hi
            self._session_low[code] = lo
            tick = Tick(
                code=code, price=price, high=hi, low=lo,
                open=prev.open, volume=rng.randint(1, 500), time=int(time.time()),
            )
            self._last_tick[code] = tick
            yield tick


class MockBroker(Broker):
    def __init__(self, data: MockDataProvider) -> None:
        self._data = data
        self._positions: Dict[str, Position] = {}
        self._order_seq = 0

    def _price(self, code: str) -> float:
        t = self._data.last_tick(code)
        if t is not None:
            return t.price
        # 틱이 아직 없으면 봉을 생성해 시가 확보
        bars = self._data.get_bars(code, 3)
        return bars[-1].close if bars else 0.0

    def _next_order_no(self) -> str:
        self._order_seq += 1
        return f"MOCK{self._order_seq:06d}"

    def position(self, code: str) -> Position:
        pos = self._positions.get(code)
        if pos is None:
            pos = Position(code=code)
            self._positions[code] = pos
        pos.current_price = self._price(code)
        return pos

    def all_positions(self) -> List[Position]:
        for code in self._positions:
            self._positions[code].current_price = self._price(code)
        return list(self._positions.values())

    def buy(self, code: str, amount_krw: int) -> OrderResult:
        price = self._price(code)
        if price <= 0:
            return OrderResult(ok=False, code=code, side="buy", filled_qty=0,
                               price=0, message="현재가 없음")
        qty = int(amount_krw // price)
        if qty <= 0:
            return OrderResult(ok=False, code=code, side="buy", filled_qty=0,
                               price=price, message="금액이 1주 단가보다 작음")
        pos = self.position(code)
        new_qty = pos.quantity + qty
        pos.avg_price = (pos.avg_price * pos.quantity + price * qty) / new_qty
        pos.quantity = new_qty
        return OrderResult(ok=True, code=code, side="buy", filled_qty=qty, price=price,
                           order_no=self._next_order_no(),
                           message=f"{qty}주 매수 체결 @ {price:,.0f}")

    def sell(self, code: str, qty: int) -> OrderResult:
        pos = self.position(code)
        if qty > pos.quantity:
            qty = pos.quantity
        if qty <= 0:
            return OrderResult(ok=False, code=code, side="sell", filled_qty=0,
                               price=0, message="매도 가능 수량 없음")
        price = self._price(code)
        pos.realized_pnl += (price - pos.avg_price) * qty
        pos.quantity -= qty
        if pos.quantity == 0:
            pos.avg_price = 0.0
        return OrderResult(ok=True, code=code, side="sell", filled_qty=qty, price=price,
                           order_no=self._next_order_no(),
                           message=f"{qty}주 매도 체결 @ {price:,.0f}")
