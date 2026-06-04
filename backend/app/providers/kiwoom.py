"""키움 실거래 어댑터 (PROVIDER=kiwoom 일 때만 활성).

`/home/rblue/work/kiwoom`의 검증된 함수들을 그대로 import 해서 재사용한다.
mock 모드에서는 이 모듈이 import 되지 않으므로 키움 의존성이 없어도 앱이 뜬다.

재사용 대상:
- get_access_token(appkey, secretkey, mock)  : OAuth 토큰
- fetch_3min_bars(token, code, date)         : 분봉 (tic_scope 확장 필요)
- place_buy_order / place_sell_order         : 주문 (kt10000/kt10001)
- WebSocket LOGIN/REG, type '0B' 필드맵       : 실시간 틱

주의: 실시간 틱 브리지는 키움 WebSocket(wss://api.kiwoom.com:10000)에 LOGIN 후
REG로 종목 구독하고 type '0B'의 values(10=현재가,17=고가,18=저가,11=거래량)를
파싱한다. 본 어댑터는 골격을 제공하며, 자격증명/네트워크가 준비된 환경에서
완성·검증해야 한다.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime
from typing import AsyncIterator, Dict, List, Optional

from ..models import Bar, OrderResult, Position, Tick
from .base import Broker, DataProvider


def _import_kiwoom():
    """kiwoom 프로젝트의 src 모듈을 sys.path에 추가하고 import."""
    kiwoom_path = os.getenv("KIWOOM_PATH", "/home/rblue/work/kiwoom")
    if kiwoom_path not in sys.path:
        sys.path.insert(0, kiwoom_path)
    # auto_trading 모듈에 핵심 함수들이 있다.
    from src import auto_trading as at  # type: ignore

    return at


# 키움 0B(주식체결) values 필드맵
F_PRICE = "10"   # 현재가
F_OPEN = "16"    # 시가
F_HIGH = "17"    # 고가
F_LOW = "18"     # 저가
F_VOL = "11"     # 체결량


class KiwoomDataProvider(DataProvider):
    def __init__(self) -> None:
        self._at = _import_kiwoom()
        appkey = os.getenv("APPKEY")
        secretkey = os.getenv("SECRETKEY")
        mock = (os.getenv("KIWOOM_MOCK", "true").lower() == "true")
        if not appkey or not secretkey:
            raise RuntimeError(
                "PROVIDER=kiwoom 인데 APPKEY/SECRETKEY 가 없습니다(.env 확인)."
            )
        self._token = self._at.get_access_token(appkey, secretkey, mock=mock)
        self._mock = mock
        self._last: Dict[str, Tick] = {}

    def get_bars(self, code: str, interval: int, lookback_extra: int = 60) -> List[Bar]:
        """ka10080 분봉 조회. fetch_3min_bars 는 tic_scope='3' 고정이므로,
        interval 을 tic_scope 로 넘기도록 직접 호출한다."""
        token = self._token
        today = datetime.now().strftime("%Y%m%d")
        # 원본 fetch_3min_bars 를 참고하되 tic_scope 를 interval 로.
        # 간단화를 위해 원함수를 재사용(3분) 후 집계하거나, 동일 패턴으로 호출.
        raw = self._fetch_bars(token, code, str(interval))
        bars: List[Bar] = []
        for r in raw:
            dt = f"{r.get('cntr_dt','')}{r.get('cntr_tm','')}"
            try:
                ts = int(datetime.strptime(dt[:12], "%Y%m%d%H%M").timestamp())
            except Exception:
                ts = int(time.time())
            bars.append(
                Bar(
                    time=ts,
                    open=abs(float(r.get("open_pric", 0))),
                    high=abs(float(r.get("high_pric", 0))),
                    low=abs(float(r.get("low_pric", 0))),
                    close=abs(float(r.get("close_pric", 0))),
                    volume=abs(float(r.get("acc_trdvol", 0))),
                )
            )
        bars.sort(key=lambda b: b.time)
        # 이전 lookback_extra + 당일 분량만큼 잘라 반환
        return bars

    def _fetch_bars(self, token: str, code: str, tic_scope: str) -> List[dict]:
        """auto_trading.fetch_3min_bars 패턴을 tic_scope 가변으로 호출."""
        import requests

        host = "https://mockapi.kiwoom.com" if self._mock else "https://api.kiwoom.com"
        url = f"{host}/api/dostk/chart"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "api-id": "ka10080",
        }
        body = {"stk_cd": code, "tic_scope": tic_scope, "upd_stkpc_tp": "1"}
        out: List[dict] = []
        cont_yn, next_key = "N", ""
        for _ in range(10):  # 페이지 제한
            h = dict(headers)
            if cont_yn == "Y":
                h["cont-yn"] = "Y"
                h["next-key"] = next_key
            resp = requests.post(url, headers=h, json=body, timeout=10)
            data = resp.json()
            out.extend(data.get("stk_min_pole_chart_qry", []) or [])
            cont_yn = resp.headers.get("cont-yn", "N")
            next_key = resp.headers.get("next-key", "")
            if cont_yn != "Y":
                break
        return out

    def last_tick(self, code: str) -> Optional[Tick]:
        return self._last.get(code)

    async def stream_ticks(self, code: str) -> AsyncIterator[Tick]:
        """키움 WebSocket(0B) 브리지. 자격증명/네트워크 준비 환경에서 검증 필요."""
        import json

        import websockets

        host = "wss://mockapi.kiwoom.com:10000" if self._mock else "wss://api.kiwoom.com:10000"
        uri = f"{host}/api/dostk/websocket"
        async with websockets.connect(uri) as wsk:
            await wsk.send(json.dumps({"trnm": "LOGIN", "token": self._token}))
            await wsk.send(json.dumps({
                "trnm": "REG", "grp_no": "1", "refresh": "1",
                "data": [{"item": [code], "type": ["0B"]}],
            }))
            async for raw in wsk:
                msg = json.loads(raw)
                if msg.get("trnm") != "REAL":
                    continue
                for d in msg.get("data", []):
                    if d.get("type") != "0B":
                        continue
                    v = d.get("values", {})
                    price = abs(float(v.get(F_PRICE, 0)))
                    tick = Tick(
                        code=code,
                        price=price,
                        high=abs(float(v.get(F_HIGH, price))),
                        low=abs(float(v.get(F_LOW, price))),
                        open=abs(float(v.get(F_OPEN, price))),
                        volume=abs(float(v.get(F_VOL, 0))),
                        time=int(time.time()),
                    )
                    self._last[code] = tick
                    yield tick


class KiwoomBroker(Broker):
    def __init__(self, data: KiwoomDataProvider) -> None:
        self._data = data
        self._at = data._at
        self._token = data._token
        self._positions: Dict[str, Position] = {}

    def _price(self, code: str) -> float:
        t = self._data.last_tick(code)
        return t.price if t else 0.0

    def buy(self, code: str, amount_krw: int) -> OrderResult:
        price = self._price(code)
        qty = int(amount_krw // price) if price > 0 else 0
        if qty <= 0:
            return OrderResult(ok=False, code=code, side="buy", filled_qty=0,
                               price=price, message="수량 0")
        order_no = self._at.place_buy_order(self._token, code, qty, order_type="3")
        ok = order_no is not None
        return OrderResult(ok=ok, code=code, side="buy", filled_qty=qty if ok else 0,
                           price=price, order_no=order_no,
                           message="매수 주문 전송" if ok else "주문 실패")

    def sell(self, code: str, qty: int) -> OrderResult:
        order_no = self._at.place_sell_order(self._token, code, qty, order_type="3")
        ok = order_no is not None
        price = self._price(code)
        return OrderResult(ok=ok, code=code, side="sell", filled_qty=qty if ok else 0,
                           price=price, order_no=order_no,
                           message="매도 주문 전송" if ok else "주문 실패")

    def position(self, code: str) -> Position:
        pos = self._positions.setdefault(code, Position(code=code))
        pos.current_price = self._price(code)
        return pos

    def all_positions(self) -> List[Position]:
        return list(self._positions.values())
