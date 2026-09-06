"""FastAPI 진입점 — REST + WebSocket (multi-account).

계좌(account)는 ``ACCOUNTS`` 환경변수로 활성화한다(기본 mock). 계좌별 종목/주문/
설정/포지션은 ``/api/{account}/...`` 로 스코핑되고, 장 시계는 전 계좌 공유라
``/api/market*`` 로 전역 제어한다. WebSocket 메시지에는 ``account`` 필드가 붙는다.
"""
from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

from .hub import Hub, manager  # noqa: E402  (load_dotenv 이후 import)
from .models import (  # noqa: E402
    AutoConfig,
    BuyOrderReq,
    OrderResult,
    SellOrderReq,
)
from .providers.base import VALID_INTERVALS  # noqa: E402

app = FastAPI(title="Bach 주식 거래 API")

# CORS — 아는 출처만 허용한다.
# 프런트는 Vite 프록시(5173)를 거쳐 상대경로로 호출하므로 브라우저는 5173하고만
# 대화한다. 즉 정상 사용 경로에는 교차 출처 요청이 아예 없다. 그런데도 '*'로
# 열어두면, 사용자가 서버를 켜둔 채 임의의 웹페이지를 방문했을 때 그 페이지의
# JS가 localhost:8000/api/real/orders/buy 로 실주문을 낼 수 있다(프런트의
# confirm 가드는 브라우저 측 코드라 이 경로에선 실행되지 않는다).
# 백엔드에 직접 붙는 개발 시나리오(/docs 등)를 위해 프런트 출처만 남긴다.
# ⚠️ CORS는 브라우저만 지키는 규칙이다. curl·스크립트에는 무력하므로 인증의
#    대체재가 아니라 값싼 방어층으로만 본다.
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# API 토큰 인증 (선택)
# ---------------------------------------------------------------------------
# API_TOKEN 이 설정돼 있으면 모든 /api/* 요청에 X-API-Token 헤더를 요구한다.
# CORS 는 브라우저만 지키는 규칙이라 curl·스크립트·다른 기기에는 무력한데,
# 이 서버는 실전 계좌 주문 API 를 노출하므로 진짜 자물쇠가 필요하다.
# 미설정이면 기존처럼 인증 없이 동작한다(mock 데모 하위호환).
# 프런트는 Vite 프록시가 헤더를 주입하므로 브라우저 코드는 바뀌지 않는다.
# WebSocket(/ws)은 브라우저 API 가 커스텀 헤더를 못 실으므로 ?token= 쿼리로 받는다.
API_TOKEN = (os.getenv("API_TOKEN") or "").strip()


@app.middleware("http")
async def _require_token(request, call_next):
    if API_TOKEN and request.url.path.startswith("/api"):
        # CORS 예비요청(OPTIONS)은 브라우저가 헤더를 싣기 전 단계라 통과시킨다.
        # 실제 요청은 어차피 토큰 검사를 다시 거친다.
        if request.method != "OPTIONS":
            import secrets
            given = request.headers.get("x-api-token") or ""
            if not secrets.compare_digest(given, API_TOKEN):
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "인증 실패(X-API-Token)"}, status_code=401)
    return await call_next(request)


def _hub(account: str) -> Hub:
    h = manager.get(account)
    if h is None:
        raise HTTPException(404, f"알 수 없는 계좌: {account}")
    return h


# ---------------------------------------------------------------------------
# 계좌 목록
# ---------------------------------------------------------------------------
@app.get("/api/accounts")
def list_accounts():
    return manager.accounts_payload()


# ---------------------------------------------------------------------------
# 종목 관리 (계좌 스코프)
# ---------------------------------------------------------------------------
@app.get("/api/{account}/stocks")
def list_stocks(account: str):
    hub = _hub(account)
    return [hub.status_of(code).model_dump() for code in hub.stocks]


@app.post("/api/{account}/stocks")
async def add_stock(account: str, payload: dict):
    hub = _hub(account)
    code = (payload.get("code") or "").strip()
    name = (payload.get("name") or "").strip()
    if not code:
        raise HTTPException(400, "code 필요")
    try:
        looked = await asyncio.to_thread(hub.data.stock_name, code)
    except Exception:
        looked = None
    if not name:
        name = looked or ""
    stock = hub.add_stock(code, name)
    return hub.status_of(stock.code).model_dump()


@app.post("/api/{account}/stocks/import-held")
async def import_held(account: str):
    """계좌 보유 종목 중 화면에 없는 것을 가져와 추가."""
    added = await _hub(account).import_held()
    return {"added": added}


@app.delete("/api/{account}/stocks/{code}")
def remove_stock(account: str, code: str):
    _hub(account).remove_stock(code)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 봉 데이터
# ---------------------------------------------------------------------------
@app.get("/api/{account}/bars")
def get_bars(
    account: str,
    code: str,
    interval: int = Query(3),
    lookback_extra: int = Query(60, ge=0, le=200),
):
    if interval not in VALID_INTERVALS:
        raise HTTPException(400, f"interval must be one of {VALID_INTERVALS}")
    bars = _hub(account).data.get_bars(code, interval, lookback_extra)
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
@app.post("/api/{account}/orders/buy", response_model=OrderResult)
async def buy(account: str, req: BuyOrderReq):
    hub = _hub(account)
    res = await asyncio.to_thread(hub.broker.buy, req.code, req.amount_krw)
    if res.ok:
        hub._log(f"[{req.code}] 수동 매수: {res.message}")
        hub.broadcast_status(req.code)
        await hub.refresh_orders()
    return res


@app.post("/api/{account}/orders/sell", response_model=OrderResult)
async def sell(account: str, req: SellOrderReq):
    hub = _hub(account)
    res = await asyncio.to_thread(hub.broker.sell, req.code, req.qty)
    if res.ok:
        hub._log(f"[{req.code}] 수동 매도: {res.message}")
        hub.broadcast_status(req.code)
        await hub.refresh_orders()
    return res


@app.get("/api/{account}/positions")
def positions(account: str):
    return [p.model_dump() for p in _hub(account).broker.all_positions()]


@app.get("/api/{account}/account")
async def account_summary(account: str):
    """계좌 요약(예수금/주문가능금액/평가금액 등). 미지원(mock) 시 null."""
    return await asyncio.to_thread(_hub(account).broker.account_summary)


# ---------------------------------------------------------------------------
# 자동매매 설정
# ---------------------------------------------------------------------------
@app.get("/api/{account}/config/{code}")
def get_config(account: str, code: str):
    stock = _hub(account).get(code)
    if not stock:
        raise HTTPException(404, "종목 없음")
    return stock.config.model_dump()


@app.put("/api/{account}/config/{code}")
def put_config(account: str, code: str, config: AutoConfig):
    hub = _hub(account)
    stock = hub.get(code)
    if not stock:
        raise HTTPException(404, "종목 없음")
    hub.set_config(code, config)
    return config.model_dump()


# ---------------------------------------------------------------------------
# 상태 전이
# ---------------------------------------------------------------------------
@app.post("/api/{account}/state/{code}/push")
def push(account: str, code: str):
    state = _hub(account).push(code)
    if state is None:
        raise HTTPException(404, "종목 없음")
    return {"code": code, "state": state.value}


# ---------------------------------------------------------------------------
# 장 이벤트 (전 계좌 공유 시계)
# ---------------------------------------------------------------------------
@app.get("/api/market")
def market_status():
    return {"phase": manager.clock.phase.value, "auto": manager.clock.auto}


def _reject_if_auto():
    if manager.clock.auto:
        raise HTTPException(status_code=409, detail="자동 장 시계(live) 모드: 수동 제어 불가")


@app.post("/api/market/open")
async def market_open():
    _reject_if_auto()
    await manager.market_open()
    return {"phase": manager.clock.phase.value, "auto": manager.clock.auto}


@app.post("/api/market/close")
def market_close():
    _reject_if_auto()
    manager.market_close()
    return {"phase": manager.clock.phase.value, "auto": manager.clock.auto}


@app.post("/api/market/reset")
def market_reset():
    _reject_if_auto()
    manager.market_reset()
    return {"phase": manager.clock.phase.value, "auto": manager.clock.auto}


@app.on_event("startup")
async def _on_startup():
    await manager.restore()  # 계좌별 저장 종목 복원
    manager.start()          # live 계좌 미체결 폴링 + 공유 KST 장 시계 가동


# ---------------------------------------------------------------------------
# WebSocket — 틱/상태/로그 브로드캐스트 (account 태그 포함)
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def ws(websocket: WebSocket):
    # 브라우저 WebSocket API 는 커스텀 헤더를 못 실으므로 쿼리 토큰으로 검사.
    if API_TOKEN:
        import secrets
        given = websocket.query_params.get("token") or ""
        if not secrets.compare_digest(given, API_TOKEN):
            await websocket.close(code=4401)  # 4xxx = 앱 정의 종료 코드
            return
    await websocket.accept()
    q = manager.subscribe()
    # 접속 직후 현재 스냅샷 전송: 계좌 목록 + 장 단계 + 계좌별 종목/틱
    await websocket.send_json({"type": "accounts", "accounts": manager.accounts_payload()})
    await websocket.send_json({
        "type": "market", "phase": manager.clock.phase.value, "auto": manager.clock.auto,
    })
    for acc, hub in manager.hubs.items():
        for code in hub.stocks:
            st = hub.status_of(code)
            if st:
                await websocket.send_json(
                    {"type": "status", "account": acc, "status": st.model_dump()})
            if st and st.recovery_notice:
                await websocket.send_json({
                    "type": "log", "account": acc,
                    "text": f"[{code}] ⚠️ {st.recovery_notice}",
                })
            lt = hub.data.last_tick(code)
            if lt is not None:
                await websocket.send_json(
                    {"type": "tick", "account": acc, "tick": lt.model_dump()})
        # 미체결 주문도 곧 갱신(블로킹 → 백그라운드)
        asyncio.create_task(hub.refresh_orders())
    try:
        while True:
            msg = await q.get()
            await websocket.send_json(msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        manager.unsubscribe(q)


@app.get("/api/health")
def health():
    return {"ok": True, "accounts": [c.id for c in manager.configs]}
