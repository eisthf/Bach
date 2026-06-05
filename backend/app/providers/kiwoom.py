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
from typing import AsyncIterator, Callable, Dict, List, Optional

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

# 00(주문체결) 실시간 FID 맵 (레퍼런스 검증)
#  함정: 체결가/체결량은 '단위'(이번 분) 914/915. 911(체결량)은 원주문 누적이라 사용 금지.
FILL_CODE = "9001"     # 종목코드
FILL_STATUS = "913"    # 주문상태: 접수/체결/확인/취소/거부
FILL_PRICE = "914"     # 단위체결가(이번 체결분)
FILL_QTY = "915"       # 단위체결량(이번 체결분)
FILL_UNFILLED = "902"  # 미체결수량
FILL_SIDE = "905"      # 주문구분: +매수 / -매도
FILL_ORDNO = "9203"    # 주문번호


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
        self._name: Dict[str, str] = {}          # 종목명 캐시
        # 실시간 0B: 키움은 앱키당 WebSocket 1개만 허용 → 단일 연결로 전 종목을
        # 멀티플렉싱한다(종목마다 연결하면 서로 LOGIN으로 밀어내 끊김 반복).
        self._queues: Dict[str, "asyncio.Queue[Tick]"] = {}  # code -> 소비 큐
        self._subscribed: set[str] = set()       # 구독 중인 종목코드
        self._ws = None                           # 활성 websocket(없으면 None)
        self._ws_task: Optional[asyncio.Task] = None
        # 주문체결(00) 이벤트 콜백. Hub가 설정한다. fill dict 또는 None(전체 갱신).
        self.on_order_fill: Optional[Callable[[Optional[dict]], None]] = None

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

    def stock_name(self, code: str) -> Optional[str]:
        # 종목 추가 시 1회: 종목명 + 초기 시세(현재가/시고저)를 ka10007로 시드.
        # 거래가 드문 종목은 첫 0B 틱까지 시간이 걸려 헤더가 '-'로 보이는데,
        # 이 시드로 추가 즉시 값이 표시되고 이후 실시간 틱이 갱신한다.
        if code not in self._name:
            q = kw.fetch_quote(self.token, code, mock=self._mock)
            if q:
                if q.get("name"):
                    self._name[code] = q["name"]
                price = q.get("price") or 0.0
                if price > 0 and code not in self._last:
                    op = q.get("open") or price
                    self._last[code] = Tick(
                        code=code, price=price,
                        high=q.get("high") or price, low=q.get("low") or price,
                        open=op, volume=0.0, time=int(time.time()),
                    )
                    if op:
                        self._day_open.setdefault(code, op)
        return self._name.get(code)

    # -- 틱 --------------------------------------------------------------
    def last_tick(self, code: str) -> Optional[Tick]:
        return self._last.get(code)

    async def stream_ticks(self, code: str) -> AsyncIterator[Tick]:
        """종목별 틱 스트림. 내부적으로 단일 공유 WS에서 0B를 라우팅한다."""
        q: "asyncio.Queue[Tick]" = asyncio.Queue(maxsize=1000)
        self._queues[code] = q
        self._subscribed.add(code)
        self._ensure_ws()
        await self._register()  # 연결돼 있으면 전체 구독을 즉시 REG
        try:
            while True:
                yield await q.get()
        finally:
            self._queues.pop(code, None)
            self._subscribed.discard(code)

    # -- 단일 공유 WebSocket 관리 -----------------------------------------
    def _ensure_ws(self) -> None:
        if self._ws_task is None or self._ws_task.done():
            self._ws_task = asyncio.create_task(self._run_ws())

    async def _register(self) -> None:
        """연결돼 있으면 현재 구독 중인 전체 종목을 한 REG로 등록.

        refresh '1'이 grp_no를 갱신(치환)할 수 있으므로, 증분이 아니라 항상
        전체 집합을 보낸다(일부만 보내면 나머지 구독이 풀릴 수 있음).
        """
        ws = self._ws
        if ws is None or not self._subscribed:
            return
        try:
            await ws.send(json.dumps({
                "trnm": "REG", "grp_no": "1", "refresh": "1",
                "data": [
                    {"item": list(self._subscribed), "type": ["0B"]},
                    {"item": [], "type": ["00"]},  # 주문체결(계좌 전체)
                ],
            }))
        except Exception:  # noqa: BLE001
            pass  # 다음 재연결 시 일괄 등록됨

    async def _run_ws(self) -> None:
        """앱키당 1개만 허용되는 실시간 소켓. 전 종목을 한 연결로 멀티플렉싱."""
        import websockets  # 지연 import (mock 모드 무의존)

        uri = kw.ws_url(self._mock)
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(uri, ping_interval=None) as ws:
                    await ws.send(json.dumps({"trnm": "LOGIN", "token": self.token}))
                    async for raw in ws:
                        msg = json.loads(raw)
                        trnm = msg.get("trnm")
                        if trnm == "LOGIN":
                            if msg.get("return_code") != 0:
                                logger.error("WS 로그인 실패: %s", msg.get("return_msg"))
                                break
                            backoff = 1.0
                            self._ws = ws
                            # 현재 구독 중인 전 종목(0B) + 주문체결(00)을 한 번에 등록
                            await ws.send(json.dumps({
                                "trnm": "REG", "grp_no": "1", "refresh": "1",
                                "data": [
                                    {"item": list(self._subscribed), "type": ["0B"]},
                                    {"item": [], "type": ["00"]},
                                ],
                            }))
                            logger.info("WS 등록 0B=%s + 00(주문체결)",
                                        ", ".join(self._subscribed) or "(없음)")
                            # 재접속 시 누락된 체결 보정: 전체 포지션 갱신 요청
                            if self.on_order_fill:
                                self.on_order_fill(None)
                        elif trnm == "PING":
                            await ws.send(raw)  # echo (keepalive)
                        elif trnm == "REAL":
                            self._dispatch_real(msg)
            except asyncio.CancelledError:
                self._ws = None
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("WS 재연결 (%.0fs 후): %s", backoff, e)
            self._ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _dispatch_real(self, msg: dict) -> None:
        """REAL 메시지를 라우팅: 0B→틱 큐, 00→주문체결 콜백."""
        for d in msg.get("data", []):
            dtype = d.get("type")
            if dtype == "00":
                self._handle_fill(d.get("values", {}))
                continue
            if dtype != "0B":
                continue
            code = d.get("item") or ""
            if code not in self._queues:
                continue
            v = d.get("values", {})
            price = kw.parse_price(v.get(F_PRICE))
            if price <= 0:
                continue
            open_ = kw.parse_price(v.get(F_OPEN)) or (self._day_open.get(code) or price)
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
            q = self._queues.get(code)
            if q is not None:
                try:
                    q.put_nowait(tick)
                except asyncio.QueueFull:
                    pass  # 소비가 느리면 최신 틱 일부 드롭(허용)

    def _handle_fill(self, v: dict) -> None:
        """00(주문체결) → 체결분만 골라 콜백 통지(평단/보유는 Hub가 계좌로 갱신)."""
        if v.get(FILL_STATUS) != "체결":
            return  # 접수/확인/취소/거부는 무시(체결만 반영)
        code = str(v.get(FILL_CODE, "")).lstrip("A").strip()
        if not code or self.on_order_fill is None:
            return
        side = "buy" if "+" in str(v.get(FILL_SIDE, "")) else "sell"
        fill = {
            "code": code,
            "side": side,
            "qty": kw.parse_int(v.get(FILL_QTY)),
            "price": kw.parse_price(v.get(FILL_PRICE)),
            "unfilled": kw.parse_int(v.get(FILL_UNFILLED)),
            "order_no": str(v.get(FILL_ORDNO, "")).strip(),
        }
        try:
            self.on_order_fill(fill)
        except Exception:  # noqa: BLE001
            logger.exception("on_order_fill 콜백 오류")


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
