"""키움 실거래 어댑터 (PROVIDER=kiwoom 일 때만 활성).

self-contained `kiwoom_api` 클라이언트만 사용한다(외부 kiwoom 프로젝트 의존 없음).
mock 모드에서는 이 모듈이 import 되지 않으므로 키움 의존성 없이 앱이 뜬다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import AsyncIterator, Dict, List, Optional

from ..models import Bar, OrderResult, Position, Tick
from . import kiwoom_api as kw
from .base import Broker, DataProvider

logger = logging.getLogger("bach.kiwoom")

# 0B(주식체결) 실시간 FID 맵 — 함정: 거래량은 15(체결량)/13(누적), 11이 아님.
F_PRICE = "10"   # 현재가(부호 포함)
F_OPEN = "16"    # 시가
F_HIGH = "17"    # 고가(당일)
F_LOW = "18"     # 저가(당일)
F_VOL = "15"     # 체결량


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


class KiwoomDataProvider(DataProvider):
    def __init__(self) -> None:
        appkey = os.getenv("APPKEY")
        secretkey = os.getenv("SECRETKEY")
        self._mock = (os.getenv("KIWOOM_MOCK", "true").lower() == "true")
        if not appkey or not secretkey:
            raise RuntimeError(
                "PROVIDER=kiwoom 인데 APPKEY/SECRETKEY 가 없습니다(.env 확인)."
            )
        self.token = kw.get_access_token(appkey, secretkey, mock=self._mock)
        self._last: Dict[str, Tick] = {}
        self._day_open: Dict[str, float] = {}    # 당일 시가(Z) 캐시
        self._prev_close: Dict[str, float] = {}  # 전일 종가(X) 캐시
        self._prev_close_day: Dict[str, str] = {}

    # -- 봉 --------------------------------------------------------------
    def get_bars(self, code: str, interval: int, lookback_extra: int = 60) -> List[Bar]:
        # rate-limit 등으로 빈 응답이 오면 짧게 쉬고 1회 재시도(빈 차트 방지).
        rows: List[dict] = []
        for attempt in range(2):
            rows = kw.fetch_min_bars(
                self.token, code, interval, mock=self._mock,
                today=_today(), lookback_extra=lookback_extra,
            )
            if rows:
                break
            if attempt == 0:
                logger.warning("[%s] 분봉 빈 응답 → 재시도", code)
                time.sleep(0.3)
        today = _today()
        bars: List[Bar] = []
        for r in rows:
            bars.append(Bar(
                time=r["time"], open=r["open"], high=r["high"],
                low=r["low"], close=r["close"], volume=r["volume"],
            ))
            # 당일 첫 봉 시가를 Z(당일 시가)로 보존
            if r["_date"] >= today and code not in self._day_open:
                self._day_open[code] = r["open"]
        return bars

    # -- X/Z (자동매매 엔진 셋업용) ---------------------------------------
    def prev_close(self, code: str) -> Optional[float]:
        today = _today()
        if self._prev_close_day.get(code) == today:
            return self._prev_close.get(code)
        x = kw.fetch_prev_close(self.token, code, mock=self._mock, today=today)
        if x:
            self._prev_close[code] = x
            self._prev_close_day[code] = today
        return x

    def day_open(self, code: str) -> Optional[float]:
        if code in self._day_open:
            return self._day_open[code]
        lt = self._last.get(code)
        return lt.open if lt and lt.open else None

    # -- 틱 --------------------------------------------------------------
    def last_tick(self, code: str) -> Optional[Tick]:
        return self._last.get(code)

    async def stream_ticks(self, code: str) -> AsyncIterator[Tick]:
        """키움 WebSocket 0B 실시간 체결 브리지. PING echo로 연결 유지."""
        import websockets  # 지연 import (mock 모드 무의존)

        uri = kw.ws_url(self._mock)
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(uri, ping_interval=None) as ws:
                    await ws.send(json.dumps({"trnm": "LOGIN", "token": self.token}))
                    registered = False
                    backoff = 1.0
                    async for raw in ws:
                        msg = json.loads(raw)
                        trnm = msg.get("trnm")
                        if trnm == "LOGIN":
                            if msg.get("return_code") != 0:
                                logger.error("[%s] WS 로그인 실패: %s", code,
                                             msg.get("return_msg"))
                                break
                            await ws.send(json.dumps({
                                "trnm": "REG", "grp_no": "1", "refresh": "1",
                                "data": [{"item": [code], "type": ["0B"]}],
                            }))
                            registered = True
                        elif trnm == "PING":
                            await ws.send(raw)  # 그대로 echo (keepalive)
                        elif trnm == "REAL":
                            for tick in self._parse_real(code, msg):
                                yield tick
                        # REG 응답 등 기타는 무시
                        if not registered:
                            continue
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("[%s] WS 재연결 (%.0fs 후): %s", code, backoff, e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _parse_real(self, code: str, msg: dict) -> List[Tick]:
        out: List[Tick] = []
        for d in msg.get("data", []):
            if d.get("type") != "0B" or d.get("item") not in (code, "", None):
                continue
            v = d.get("values", {})
            price = kw.parse_price(v.get(F_PRICE))
            if price <= 0:
                continue
            open_ = kw.parse_price(v.get(F_OPEN)) or (
                self._day_open.get(code) or price)
            if code not in self._day_open and open_:
                self._day_open[code] = open_
            tick = Tick(
                code=code,
                price=price,
                high=kw.parse_price(v.get(F_HIGH)) or price,
                low=kw.parse_price(v.get(F_LOW)) or price,
                open=open_,
                volume=kw.parse_price(v.get(F_VOL)),
                time=int(time.time()),
            )
            self._last[code] = tick
            out.append(tick)
        return out


class KiwoomBroker(Broker):
    """주문 실행 + 계좌 기반 포지션 조회.

    포지션은 키움 계좌(kt00018)를 단일 진실원으로 삼는다(짧은 TTL 캐시).
    """

    _POS_TTL = 2.0  # seconds

    def __init__(self, data: KiwoomDataProvider) -> None:
        self._data = data
        self._mock = data._mock
        self._pos_cache: Dict[str, dict] = {}
        self._pos_ts = 0.0

    def _price(self, code: str) -> float:
        t = self._data.last_tick(code)
        return t.price if t else 0.0

    def invalidate(self) -> None:
        self._pos_ts = 0.0

    def _positions(self, force: bool = False) -> Dict[str, dict]:
        now = time.time()
        if force or now - self._pos_ts > self._POS_TTL:
            self._pos_cache = kw.fetch_positions(self._data.token, mock=self._mock)
            self._pos_ts = now
        return self._pos_cache

    def buy(self, code: str, amount_krw: int) -> OrderResult:
        price = self._price(code)
        qty = int(amount_krw // price) if price > 0 else 0
        if qty <= 0:
            return OrderResult(ok=False, code=code, side="buy", filled_qty=0,
                               price=price, message="현재가 없음 또는 금액 부족")
        order_no = kw.place_order(self._data.token, code, qty, "buy",
                                  mock=self._mock, order_type="3")
        ok = order_no is not None
        if ok:
            self._pos_ts = 0.0  # 다음 조회 시 갱신
        return OrderResult(ok=ok, code=code, side="buy", filled_qty=qty if ok else 0,
                           price=price, order_no=order_no,
                           message=f"{qty}주 시장가 매수 전송" if ok else "주문 실패")

    def sell(self, code: str, qty: int) -> OrderResult:
        order_no = kw.place_order(self._data.token, code, qty, "sell",
                                  mock=self._mock, order_type="3")
        ok = order_no is not None
        price = self._price(code)
        if ok:
            self._pos_ts = 0.0
        return OrderResult(ok=ok, code=code, side="sell", filled_qty=qty if ok else 0,
                           price=price, order_no=order_no,
                           message=f"{qty}주 시장가 매도 전송" if ok else "주문 실패")

    def position(self, code: str) -> Position:
        p = self._positions().get(code)
        if not p:
            return Position(code=code, current_price=self._price(code))
        return Position(
            code=code,
            quantity=p["quantity"],
            avg_price=p["avg_price"],
            current_price=self._price(code) or p["current_price"],
        )

    def all_positions(self) -> List[Position]:
        return [self.position(code) for code in self._positions(force=True)]
