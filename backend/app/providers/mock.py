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
import calendar
import random
import zlib
from datetime import timedelta
from typing import AsyncIterator, Dict, List

from ..market_clock import (
    chart_epoch, now_kst, prev_session_close_epoch, session_open_epoch)
from ..models import Bar, OrderResult, Position, Tick
from ..pricing import tick_size
from .base import Broker, DAY_INTERVAL, DataProvider

def round_to_tick(price: float) -> float:
    t = tick_size(price)
    return round(price / t) * t


def _seed_for(code: str) -> int:
    """종목코드 → 시드. **프로세스 간에도 안정적**이어야 한다.

    파이썬의 str.hash 는 PYTHONHASHSEED 로 랜덤화되어 실행마다 값이 달라진다.
    그래서 예전엔 재시작할 때마다 같은 종목의 차트·시나리오가 통째로 바뀌어,
    "어제 본 그 상황을 다시 재현"이 불가능했다. crc32 는 결정적이다.
    """
    return zlib.crc32(f"bach-mock:{code}".encode()) % (2**31)


# 당일 정규장 분 수(09:00~15:30 = 390분)
SESSION_MINUTES = 390


def _bars_elapsed(interval: int) -> int:
    """지금까지 형성됐을 당일 봉 수(장전 0, 장마감 후 전체).

    live(ka10080)가 '당일 봉 지금까지'만 주는 것과 같은 모양을 만든다. 예전엔
    시각과 무관하게 항상 하루치를 통째로 생성해, 같은 코드가 mock/live 에서
    정반대로 동작했다(hub._start_auto 의 X/Z 산정이 mock 에서만 맞았던 원인).
    """
    per_day = max(1, SESSION_MINUTES // interval)
    elapsed_min = (chart_epoch() - session_open_epoch()) // 60
    if elapsed_min < 0:
        return 0
    return max(0, min(per_day, int(elapsed_min) // interval + 1))


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
        if interval == DAY_INTERVAL:
            return self._get_day_bars(code, lookback_extra)
        rng = random.Random(_seed_for(code) ^ (interval * 2654435761 & 0xFFFFFFFF))

        x = self._prev_close[code]
        z = self._open_price[code]

        # 당일 봉은 '지금까지' 형성된 만큼만(live 와 동일한 모양).
        bars_per_day = _bars_elapsed(interval)

        # 당일 첫 봉의 SMA60을 위해 이전 lookback_extra개 봉을 앞에 붙인다.
        # 이 구간은 X(전일 종가) 근처에서 수렴하도록 역방향 워크.
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
        if not closes:
            return []

        bars: List[Bar] = []
        interval_sec = interval * 60
        # 실제 날짜에 앵커링해야 틱(chart_epoch)과 같은 축에 놓여, 프런트가
        # "이 틱이 어느 봉인가"를 계산할 수 있다.
        #   - 이전 구간: 직전 거래일 15:30 에서 끝나도록 뒤에서부터 배치
        #   - 당일 구간: 오늘 09:00 에서 시작
        # 이전 구간을 오늘 09:00 바로 앞에 붙이면, 장전(00:00~09:00)에는 그
        # 봉들이 '미래'에 놓여 마지막 봉이 현재 틱보다 뒤가 된다(롤오버 판정이
        # 깨진다). live 의 ka10080 도 전일 봉을 전일 시각에 준다.
        day_open_t = session_open_epoch()
        prev_close_t = prev_session_close_epoch()
        prev_close = closes[0]
        for i, close in enumerate(closes):
            o = prev_close
            # intrabar 변동
            hi = max(o, close) * (1 + abs(rng.uniform(0, 0.01)))
            lo = min(o, close) * (1 - abs(rng.uniform(0, 0.01)))
            hi = round_to_tick(hi)
            lo = round_to_tick(max(lo, tick_size(lo)))
            vol = rng.randint(1_000, 100_000)
            if i < lookback_extra:
                t = prev_close_t - (lookback_extra - i) * interval_sec
            else:
                t = day_open_t + (i - lookback_extra) * interval_sec
            bars.append(
                Bar(
                    time=t,
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
        # 장중 고가/저가는 '당일 봉 전체'에서 구한다. 예전엔 마지막 봉 종가로
        # 초기화해, 헤더의 고가/저가가 실제로는 '앱을 켠 뒤의' 값이라 차트가
        # 보여주는 당일 범위와 어긋났다(live 의 FID 17/18 은 진짜 당일 고저다).
        day_bars = bars[lookback_extra:] or bars
        self._session_high[code] = max(b.high for b in day_bars)
        self._session_low[code] = min(b.low for b in day_bars)
        self._last_tick[code] = Tick(
            code=code, price=last_close,
            high=self._session_high[code], low=self._session_low[code],
            open=z, volume=0, time=chart_epoch(),
        )
        return bars

    def _get_day_bars(self, code: str, lookback_extra: int) -> List[Bar]:
        """주말을 제외한 이전 일봉과 오늘 진행봉을 결정적으로 생성한다."""
        now = now_kst()
        include_today = now.weekday() < 5 and now.hour >= 9
        previous_count = lookback_extra if include_today else lookback_extra + 1
        dates = []
        day = now.date() - timedelta(days=1)
        while len(dates) < previous_count:
            if day.weekday() < 5:
                dates.append(day)
            day -= timedelta(days=1)
        dates.reverse()
        if include_today:
            dates.append(now.date())
        if not dates:
            return []

        rng = random.Random(_seed_for(code) ^ 0x1DA7B4)
        x = self._prev_close[code]
        z = self._open_price[code]

        # 과거 구간의 마지막 종가가 전략 기준가 X와 일치하도록 뒤에서 생성한다.
        historical = []
        price = x
        for _ in range(previous_count):
            historical.append(price)
            price = round_to_tick(price / (1 + rng.uniform(-0.025, 0.025)))
        historical.reverse()

        bars: List[Bar] = []
        previous_close = historical[0] if historical else x
        for day, close in zip(dates[:previous_count], historical):
            opened = previous_close
            high = round_to_tick(max(opened, close) * (1 + rng.uniform(0, 0.018)))
            low = round_to_tick(min(opened, close) * (1 - rng.uniform(0, 0.018)))
            bars.append(Bar(
                time=calendar.timegm(day.timetuple()),
                open=opened,
                high=high,
                low=max(low, tick_size(low)),
                close=close,
                volume=rng.randint(100_000, 5_000_000),
            ))
            previous_close = close

        if include_today:
            tick = self._last_tick.get(code)
            close = tick.price if tick else z
            high = tick.high if tick else max(z, close)
            low = tick.low if tick else min(z, close)
            bars.append(Bar(
                time=calendar.timegm(now.date().timetuple()),
                open=z,
                high=high,
                low=low,
                close=close,
                volume=tick.volume if tick else 0,
            ))
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
            now = chart_epoch()
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
                open=prev.open, volume=rng.randint(1, 500), time=chart_epoch(),
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
