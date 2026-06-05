"""종목 허브 — 종목별 상태/설정/틱태스크/자동매매 엔진을 총괄.

서버가 상태머신의 권위를 가지며, 틱 스트림과 이벤트(체결/로그/상태변화)를
연결된 WebSocket 클라이언트에 브로드캐스트한다.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .market_clock import MarketClock
from .models import (
    AutoConfig,
    MarketPhase,
    Position,
    StockStatus,
    Tick,
    TradeState,
)
from .providers import build_provider
from .state_machine import StateMachine
from .strategy.ulc import UlcEngine


@dataclass
class Stock:
    code: str
    name: str
    machine: StateMachine
    config: AutoConfig
    task: Optional[asyncio.Task] = None
    engine: Optional[UlcEngine] = None


class Hub:
    def __init__(self) -> None:
        self.clock = MarketClock()
        self.data, self.broker = build_provider()
        self.stocks: Dict[str, Stock] = {}
        self._clients: Set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    # -- WebSocket pub/sub -------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._clients.discard(q)

    def broadcast(self, msg: dict) -> None:
        dead = []
        for q in self._clients:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._clients.discard(q)

    def _log(self, text: str) -> None:
        self.broadcast({"type": "log", "text": text})

    # -- 종목 관리 ---------------------------------------------------------
    def add_stock(self, code: str, name: str = "") -> Stock:
        if code in self.stocks:
            return self.stocks[code]
        stock = Stock(
            code=code,
            name=name or code,
            machine=StateMachine(self.clock),
            config=AutoConfig(),
        )
        self.stocks[code] = stock
        stock.task = asyncio.create_task(self._tick_loop(stock))
        self._log(f"[{code}] 종목 추가됨")
        self.broadcast_status(code)
        return stock

    def remove_stock(self, code: str) -> None:
        stock = self.stocks.pop(code, None)
        if stock and stock.task:
            stock.task.cancel()
        self._log(f"[{code}] 종목 제거됨")

    def get(self, code: str) -> Optional[Stock]:
        return self.stocks.get(code)

    # -- 상태 직렬화 -------------------------------------------------------
    def status_of(self, code: str) -> Optional[StockStatus]:
        stock = self.stocks.get(code)
        if not stock:
            return None
        pos = self.broker.position(code)
        return StockStatus(
            code=code,
            name=stock.name,
            state=stock.machine.state,
            config=stock.config,
            position=pos,
        )

    def broadcast_status(self, code: str) -> None:
        st = self.status_of(code)
        if st:
            self.broadcast({"type": "status", "status": st.model_dump()})

    # -- 틱 루프 (종목별) --------------------------------------------------
    async def _tick_loop(self, stock: Stock) -> None:
        code = stock.code
        try:
            async for tick in self.data.stream_ticks(code):
                # 브로커 현재가 갱신용으로 포지션 평가
                self.broadcast({"type": "tick", "tick": tick.model_dump()})
                # 자동매매: AUTO_TRADING 상태이고 엔진이 있으면 틱 위임
                if stock.machine.state == TradeState.AUTO_TRADING and stock.engine:
                    self._run_engine_tick(stock, tick)
        except asyncio.CancelledError:
            pass

    def _run_engine_tick(self, stock: Stock, tick: Tick) -> None:
        eng = stock.engine
        if eng is None:
            return
        before_shares = eng.shares

        def buy_fn(amount: int) -> float:
            res = self.broker.buy(stock.code, amount)
            return res.price if res.ok else 0.0

        def sell_fn(qty: int) -> float:
            res = self.broker.sell(stock.code, qty)
            return res.price if res.ok else 0.0

        eng.on_tick(tick, buy_fn, sell_fn)
        # 포지션 변화가 있었으면 상태 브로드캐스트
        if eng.shares != before_shares:
            self.broadcast_status(stock.code)
        # 엔진이 청산 완료(DONE)면 자동으로 LIQUIDATE 전이
        from .strategy.ulc import Phase

        if eng.phase == Phase.DONE and stock.machine.state == TradeState.AUTO_TRADING:
            stock.machine.on_position_flat()
            stock.engine = None
            self._log(f"[{stock.code}] 자동매매 청산 완료(보유수량 0) → MANUAL_TRADING")
            self.broadcast_status(stock.code)

    # -- 상태 전이 이벤트 --------------------------------------------------
    def push(self, code: str) -> Optional[TradeState]:
        stock = self.stocks.get(code)
        if not stock:
            return None
        new_state = stock.machine.push()
        # AUTO_TRADING을 사람이 인수(PUSH)하면 자동매매 엔진을 정리한다.
        # 이후 매도는 수동매매 패널에서 사람이 직접 한다.
        if new_state == TradeState.MANUAL_TRADING:
            stock.engine = None
        self._log(f"[{code}] PUSH → {new_state.value}")
        self.broadcast_status(code)
        return new_state

    def set_config(self, code: str, config: AutoConfig) -> None:
        stock = self.stocks.get(code)
        if stock:
            stock.config = config
            self.broadcast_status(code)

    # -- 장 이벤트 ---------------------------------------------------------
    def market_open(self) -> None:
        self.clock.open()
        self._log("📈 장 시작 (MARKET-OPEN)")
        for stock in self.stocks.values():
            prev = stock.machine.state
            new = stock.machine.on_market_open()
            if prev == TradeState.MONITOR and new == TradeState.AUTO_TRADING:
                self._start_auto(stock)
            self.broadcast_status(stock.code)
        self.broadcast_market()

    def market_close(self) -> None:
        # 장 종료 = 하루 거래 사이클의 끝. 다음 거래일 장전(PRE_OPEN) +
        # 수동매매 초기 상태로 리셋하여 다시 MONITOR 진입이 가능하게 한다.
        self.clock.reset()
        self._log("📉 장 종료 (MARKET-CLOSE) → 장전·수동매매 초기 상태로 리셋")
        for stock in self.stocks.values():
            stock.machine.on_market_close()
            stock.engine = None
            self.broadcast_status(stock.code)
        self.broadcast_market()

    def market_reset(self) -> None:
        self.clock.reset()
        for stock in self.stocks.values():
            stock.machine.state = TradeState.MANUAL_TRADING
            stock.engine = None
            self.broadcast_status(stock.code)
        self._log("⏮ 장 상태 초기화 (PRE_OPEN)")
        self.broadcast_market()

    def broadcast_market(self) -> None:
        self.broadcast({"type": "market", "phase": self.clock.phase.value})

    def _start_auto(self, stock: Stock) -> None:
        """MONITOR → AUTO_TRADING 진입 시 ULC 엔진 셋업."""
        # 봉을 가져와 X(전일 종가)·Z(당일 시가) 확보
        bars = self.data.get_bars(stock.code, 3)
        # 이전 60봉 다음이 당일 첫 봉. mock 기준: prev_close=X, 당일 시가=Z
        prev_close = bars[-(390 // 3) - 1].close if len(bars) > (390 // 3) else bars[0].close
        day_open = bars[-(390 // 3)].open if len(bars) > (390 // 3) else bars[-1].open
        # mock provider는 last_tick.open 에 Z를 둔다. 우선 그것을 신뢰.
        lt = self.data.last_tick(stock.code)
        z = lt.open if lt else day_open
        x = prev_close
        eng = UlcEngine(
            code=stock.code,
            config=stock.config,
            x=float(x),
            z=float(z),
            log=self._log,
        )
        eng.setup()
        stock.engine = eng
        self._log(f"[{stock.code}] 자동매매 시작 (X={x:,.0f}, Z={z:,.0f})")


hub = Hub()
