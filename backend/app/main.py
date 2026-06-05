"""FastAPI 진입점 — REST + WebSocket.

PROVIDER 환경변수(mock|kiwoom)로 데이터/주문 소스를 선택한다(기본 mock).
"""
from __future__ import annotations

import asyncio

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

from .hub import hub  # noqa: E402  (load_dotenv 이후 import)
from .models import (  # noqa: E402
    AutoConfig,
    BuyOrderReq,
    OrderResult,
    SellOrderReq,
)
from .providers.base import VALID_INTERVALS  # noqa: E402

app = FastAPI(title="Bach 주식 거래 API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 종목 관리
# ---------------------------------------------------------------------------
@app.get("/api/stocks")
def list_stocks():
    return [hub.status_of(code).model_dump() for code in hub.stocks]


@app.post("/api/stocks")
async def add_stock(payload: dict):
    code = (payload.get("code") or "").strip()
    name = (payload.get("name") or "").strip()
    if not code:
        raise HTTPException(400, "code 필요")
    stock = hub.add_stock(code, name)
    return hub.status_of(stock.code).model_dump()


@app.delete("/api/stocks/{code}")
def remove_stock(code: str):
    hub.remove_stock(code)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 봉 데이터
# ---------------------------------------------------------------------------
@app.get("/api/bars")
def get_bars(
    code: str,
    interval: int = Query(3),
    lookback_extra: int = Query(60, ge=0, le=200),
):
    if interval not in VALID_INTERVALS:
        raise HTTPException(400, f"interval must be one of {VALID_INTERVALS}")
    bars = hub.data.get_bars(code, interval, lookback_extra)
    # 당일 첫 봉 인덱스(이전 lookback_extra개 다음). 프런트가 visible range 설정에 활용.
    return {
        "code": code,
        "interval": interval,
        "lookback_extra": lookback_extra,
        "day_start_index": lookback_extra,
        "bars": [b.model_dump() for b in bars],
    }


# ---------------------------------------------------------------------------
# 주문 / 포지션
# ---------------------------------------------------------------------------
@app.post("/api/orders/buy", response_model=OrderResult)
async def buy(req: BuyOrderReq):
    # 블로킹 REST 주문은 스레드로 오프로드(이벤트 루프 차단 방지)
    res = await asyncio.to_thread(hub.broker.buy, req.code, req.amount_krw)
    if res.ok:
        hub._log(f"[{req.code}] 수동 매수: {res.message}")
        hub.broadcast_status(req.code)
        hub.schedule_order_refresh(req.code)  # 체결 반영 폴링
    return res


@app.post("/api/orders/sell", response_model=OrderResult)
async def sell(req: SellOrderReq):
    res = await asyncio.to_thread(hub.broker.sell, req.code, req.qty)
    if res.ok:
        hub._log(f"[{req.code}] 수동 매도: {res.message}")
        hub.broadcast_status(req.code)
        hub.schedule_order_refresh(req.code)  # 체결 반영 폴링
    return res


@app.get("/api/positions")
def positions():
    return [p.model_dump() for p in hub.broker.all_positions()]


# ---------------------------------------------------------------------------
# 자동매매 설정
# ---------------------------------------------------------------------------
@app.get("/api/config/{code}")
def get_config(code: str):
    stock = hub.get(code)
    if not stock:
        raise HTTPException(404, "종목 없음")
    return stock.config.model_dump()


@app.put("/api/config/{code}")
def put_config(code: str, config: AutoConfig):
    stock = hub.get(code)
    if not stock:
        raise HTTPException(404, "종목 없음")
    hub.set_config(code, config)
    return config.model_dump()


# ---------------------------------------------------------------------------
# 상태 전이
# ---------------------------------------------------------------------------
@app.post("/api/state/{code}/push")
def push(code: str):
    state = hub.push(code)
    if state is None:
        raise HTTPException(404, "종목 없음")
    return {"code": code, "state": state.value}


# ---------------------------------------------------------------------------
# 장 이벤트 (mock 제어)
# ---------------------------------------------------------------------------
@app.get("/api/market")
def market_status():
    return {"phase": hub.clock.phase.value, "auto": hub.clock.auto}


def _reject_if_auto():
    # live(자동 시계) 모드에선 수동 장 제어를 막는다(실제 KST 시각이 진실원).
    if hub.clock.auto:
        raise HTTPException(status_code=409, detail="자동 장 시계(live) 모드: 수동 제어 불가")


@app.post("/api/market/open")
def market_open():
    _reject_if_auto()
    hub.market_open()
    return {"phase": hub.clock.phase.value, "auto": hub.clock.auto}


@app.post("/api/market/close")
def market_close():
    _reject_if_auto()
    hub.market_close()
    return {"phase": hub.clock.phase.value, "auto": hub.clock.auto}


@app.post("/api/market/reset")
def market_reset():
    _reject_if_auto()
    hub.market_reset()
    return {"phase": hub.clock.phase.value, "auto": hub.clock.auto}


@app.on_event("startup")
async def _on_startup():
    hub.start_clock()  # live 모드면 실시간 KST 장 시계 가동


# ---------------------------------------------------------------------------
# WebSocket — 틱/상태/로그 브로드캐스트
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    q = hub.subscribe()
    # 접속 직후 현재 스냅샷 전송
    await websocket.send_json({
        "type": "market", "phase": hub.clock.phase.value, "auto": hub.clock.auto,
    })
    for code in hub.stocks:
        st = hub.status_of(code)
        if st:
            await websocket.send_json({"type": "status", "status": st.model_dump()})
    try:
        while True:
            msg = await q.get()
            await websocket.send_json(msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        hub.unsubscribe(q)


@app.get("/api/health")
def health():
    import os

    return {"ok": True, "provider": (os.getenv("PROVIDER") or "mock")}
