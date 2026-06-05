"""종목 허브 — 종목별 상태/설정/틱태스크/자동매매 엔진을 총괄.

서버가 상태머신의 권위를 가지며, 틱 스트림과 이벤트(체결/로그/상태변화)를
연결된 WebSocket 클라이언트에 브로드캐스트한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger("bach.hub")

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
        import os
        self.live = (os.getenv("PROVIDER", "mock").strip().lower() == "kiwoom")
        # live: 실제 KST 시계 자동 / mock: 수동 토글
        self.clock = MarketClock(auto=self.live)
        self.data, self.broker = build_provider()
        self.stocks: Dict[str, Stock] = {}
        self._clients: Set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()
        self._clock_task: Optional[asyncio.Task] = None
        # 종목/설정 영속화 파일(backend/state.json). cwd와 무관하게 절대경로.
        self._state_path = Path(
            os.getenv("BACH_STATE")
            or (Path(__file__).resolve().parent.parent / "state.json")
        )
        self._restoring = False  # 복원 중엔 저장 억제
        # 주문체결(00) 이벤트 → 포지션 즉시 갱신(폴링 대신 이벤트 기반).
        if hasattr(self.data, "on_order_fill"):
            self.data.on_order_fill = self._on_order_fill

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
        self._persist()
        self._log(f"[{code}] 종목 추가됨")
        self.broadcast_status(code)
        # 추가 즉시 시세 표시: provider가 시드한 초기 틱이 있으면 브로드캐스트
        # (실시간 0B 첫 틱이 늦은 비유동 종목도 헤더가 '-'로 남지 않게)
        lt = self.data.last_tick(code)
        if lt is not None:
            self.broadcast({"type": "tick", "tick": lt.model_dump()})
        return stock

    def remove_stock(self, code: str) -> None:
        stock = self.stocks.pop(code, None)
        if stock and stock.task:
            stock.task.cancel()
        self._persist()
        self._log(f"[{code}] 종목 제거됨")

    def get(self, code: str) -> Optional[Stock]:
        return self.stocks.get(code)

    # -- 영속화 (종목 + 자동매매 설정) ------------------------------------
    def _persist(self) -> None:
        """종목 목록과 설정을 디스크(JSON)에 저장. 복원 중에는 억제."""
        if self._restoring:
            return
        data = {
            "stocks": [
                {"code": s.code, "name": s.name, "config": s.config.model_dump()}
                for s in self.stocks.values()
            ]
        }
        try:
            self._state_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("상태 저장 실패(%s): %s", self._state_path, e)

    def _load_state(self) -> Dict[str, dict]:
        """저장된 상태를 {code: {name, config}} 로 로드. 없으면 빈 dict."""
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as e:  # noqa: BLE001
            logger.warning("상태 로드 실패(%s): %s", self._state_path, e)
            return {}
        out: Dict[str, dict] = {}
        for s in raw.get("stocks", []) or []:
            code = str(s.get("code", "")).strip()
            if code:
                out[code] = {"name": s.get("name") or "", "config": s.get("config")}
        return out

    async def restore(self) -> None:
        """startup 시 1회: 마지막으로 화면에 표시됐던 종목 목록(JSON)을 복원.

        계좌 보유 종목은 자동 병합하지 않는다(사용자가 '보유 종목 가져오기'로
        명시적으로 가져온다 → import_held).
        """
        saved = self._load_state()
        if not saved:
            return
        self._restoring = True
        try:
            for code, entry in saved.items():
                name = entry.get("name") or ""
                if not name:
                    try:
                        name = await asyncio.to_thread(self.data.stock_name, code) or ""
                    except Exception:  # noqa: BLE001
                        name = ""
                stock = self.add_stock(code, name)
                cfg = entry.get("config")
                if cfg:
                    try:
                        stock.config = AutoConfig(**cfg)
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            self._restoring = False
        self._log(f"종목 복원: {len(saved)}개")

    async def import_held(self) -> List[str]:
        """계좌(kt00018) 보유 종목 중 화면에 없는 것을 가져와 추가. 추가 코드 반환."""
        try:
            positions = await asyncio.to_thread(self.broker.all_positions)
        except Exception as e:  # noqa: BLE001
            logger.warning("보유 종목 조회 실패: %s", e)
            return []
        added: List[str] = []
        for p in positions:
            if p.quantity <= 0 or p.code in self.stocks:
                continue
            name = ""
            try:
                name = await asyncio.to_thread(self.data.stock_name, p.code) or ""
            except Exception:  # noqa: BLE001
                name = ""
            self.add_stock(p.code, name)
            added.append(p.code)
        if added:
            self._log(f"보유 종목 가져오기: {len(added)}개 추가 ({', '.join(added)})")
        return added

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

    # -- 주문체결(00) 이벤트 기반 포지션 갱신 -----------------------------
    def _on_order_fill(self, fill: Optional[dict]) -> None:
        """실시간 주문체결 콜백. fill=None이면 전체 갱신(재접속 보정)."""
        self.broker.invalidate()  # 계좌 캐시 무효화 → 다음 조회 시 강제 재조회
        if fill is None:
            asyncio.create_task(self._refresh_all_positions())
            return
        code = fill.get("code")
        if not code or code not in self.stocks:
            return
        side = "매수" if fill.get("side") == "buy" else "매도"
        self._log(
            f"[{code}] 체결: {side} {fill.get('qty')}주 @ "
            f"{fill.get('price', 0):,.0f} (미체결 {fill.get('unfilled')})"
        )
        asyncio.create_task(self._refresh_status(code))

    async def _refresh_status(self, code: str) -> None:
        await asyncio.to_thread(self.broker.position, code)  # 블로킹 조회 오프로드
        self.broadcast_status(code)

    async def _refresh_all_positions(self) -> None:
        await asyncio.to_thread(self.broker.all_positions)   # 1회 강제 조회로 캐시 채움
        for code in list(self.stocks.keys()):
            self.broadcast_status(code)

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
            self._persist()
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
        self.broadcast({
            "type": "market",
            "phase": self.clock.phase.value,
            "auto": self.clock.auto,
        })

    # -- 실시간 KST 자동 시계 (live 모드) ----------------------------------
    def start_clock(self) -> None:
        """live 모드에서 실제 시각으로 장 단계를 감시하는 루프를 1회 기동."""
        if not self.live or self._clock_task is not None:
            return
        self._clock_task = asyncio.create_task(self._auto_clock_loop())

    async def _auto_clock_loop(self) -> None:
        """KST 시각으로 phase 경계를 감지해 MARKET-OPEN/CLOSE를 자동 발생."""
        last = self.clock.phase
        self._log(f"🕘 실시간 장 시계 시작 (현재 단계: {last.value})")
        self.broadcast_market()
        while True:
            try:
                await asyncio.sleep(5)
                cur = self.clock.phase
                if cur == last:
                    continue
                if last == MarketPhase.PRE_OPEN and cur == MarketPhase.OPEN:
                    self.market_open()        # MONITOR→AUTO, 엔진 시작
                elif last == MarketPhase.OPEN and cur == MarketPhase.CLOSED:
                    self.market_close()       # 전 종목 수동 복귀
                else:
                    # CLOSED→PRE_OPEN(자정 등) 등은 단계 갱신만
                    self.broadcast_market()
                last = cur
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                self._log(f"⚠️ 장 시계 오류: {e}")

    def _start_auto(self, stock: Stock) -> None:
        """MONITOR → AUTO_TRADING 진입 시 ULC 엔진 셋업."""
        # 봉을 미리 한 번 당겨 provider 캐시(X/Z) 채움
        bars = self.data.get_bars(stock.code, 3)
        # X(전일 종가)·Z(당일 시가): provider가 직접 제공하면 신뢰(실거래 정확).
        # 없으면 봉/틱에서 추정(mock 호환).
        x = self.data.prev_close(stock.code)
        z = self.data.day_open(stock.code)
        if x is None:
            x = bars[-(390 // 3) - 1].close if len(bars) > (390 // 3) else bars[0].close
        if z is None:
            lt = self.data.last_tick(stock.code)
            z = lt.open if lt else (
                bars[-(390 // 3)].open if len(bars) > (390 // 3) else bars[-1].open)
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
