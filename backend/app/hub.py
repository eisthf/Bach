"""계좌 허브 — multi-account.

``AccountManager`` 가 여러 계좌(예: 모의투자 + 실전)를 총괄한다:
- 장 시계(MarketClock)는 **공유** (두 계좌가 동일한 KST 장 시각을 따른다).
- WebSocket 클라이언트 버스(broadcast)도 공유 — 메시지에 ``account`` 필드를 달아
  프런트가 계좌별로 라우팅한다.
- 계좌마다 ``Hub`` 가 종목/상태머신/틱태스크/자동매매 엔진/미체결/영속화를 담당한다.

서버가 상태머신의 권위를 가지며, 틱 스트림과 이벤트(체결/로그/상태변화)를
연결된 WebSocket 클라이언트에 (계좌 태그와 함께) 브로드캐스트한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger("bach.hub")

from .accounts import AccountConfig, load_account_configs
from .market_clock import MarketClock
from .models import (
    AutoConfig,
    MarketPhase,
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
    """단일 계좌의 종목/매매를 담당. 시계와 WS 버스는 manager에서 공유."""

    def __init__(self, cfg: AccountConfig, clock: MarketClock, manager: "AccountManager") -> None:
        self.account = cfg.id
        self.label = cfg.label
        self.danger = cfg.danger
        self.cfg = cfg
        self.clock = clock          # 공유 시계
        self.manager = manager      # WS 버스 + 글로벌 로그
        self.live = (cfg.provider == "kiwoom")
        self.data, self.broker = build_provider(cfg)
        self.stocks: Dict[str, Stock] = {}
        self._lock = asyncio.Lock()
        self._orders_task: Optional[asyncio.Task] = None
        # 계좌별 영속화 파일. mock 계좌는 하위호환을 위해 기존 state.json 사용.
        base = Path(__file__).resolve().parent.parent
        self._state_path = Path(
            os.getenv(f"BACH_STATE_{cfg.id.upper()}")
            or (base / ("state.json" if cfg.id == "mock" else f"state.{cfg.id}.json"))
        )
        self._restoring = False
        # 주문체결(00) 이벤트 → 포지션 즉시 갱신(폴링 대신 이벤트 기반).
        if hasattr(self.data, "on_order_fill"):
            self.data.on_order_fill = self._on_order_fill

    # -- 브로드캐스트 (계좌 태그 주입) ------------------------------------
    def broadcast(self, msg: dict) -> None:
        self.manager.broadcast({**msg, "account": self.account})

    def _log(self, text: str) -> None:
        # 로그는 계좌 라벨을 접두로 달아 한 패널에서 구분 가능하게.
        self.manager.broadcast({"type": "log", "text": text, "account": self.account})

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
        """startup 시 1회: 마지막으로 화면에 표시됐던 종목 목록(JSON)을 복원."""
        saved = self._load_state()
        if not saved:
            return
        self._restoring = True
        try:
            for code, entry in saved.items():
                fetched = None
                try:
                    fetched = await asyncio.to_thread(self.data.stock_name, code)
                except Exception:  # noqa: BLE001
                    pass
                name = entry.get("name") or fetched or ""
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
        self.broker.invalidate()
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
        asyncio.create_task(self.refresh_orders())

    async def _refresh_status(self, code: str) -> None:
        await asyncio.to_thread(self.broker.position, code)
        self.broadcast_status(code)

    async def _refresh_all_positions(self) -> None:
        await asyncio.to_thread(self.broker.all_positions)
        for code in list(self.stocks.keys()):
            self.broadcast_status(code)
        await self.refresh_orders()

    # -- 미체결 주문 ------------------------------------------------------
    async def refresh_orders(self) -> None:
        """계좌 미체결 주문(ka10075)을 조회해 종목별로 브로드캐스트."""
        try:
            orders = await asyncio.to_thread(self.broker.unfilled_orders)
        except Exception as e:  # noqa: BLE001
            logger.warning("미체결 조회 실패: %s", e)
            return
        self.broadcast({"type": "orders", "orders": orders})

    async def _orders_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(5)
                if self.stocks:
                    await self.refresh_orders()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.warning("미체결 루프 오류: %s", e)

    # -- 틱 루프 (종목별) --------------------------------------------------
    async def _tick_loop(self, stock: Stock) -> None:
        code = stock.code
        try:
            async for tick in self.data.stream_ticks(code):
                self.broadcast({"type": "tick", "tick": tick.model_dump()})
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
        if eng.shares != before_shares:
            self.broadcast_status(stock.code)
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

    # -- 장 이벤트 적용 (manager가 전 계좌에 대해 호출) --------------------
    def apply_market_open(self) -> None:
        for stock in self.stocks.values():
            prev = stock.machine.state
            new = stock.machine.on_market_open()
            if prev == TradeState.MONITOR and new == TradeState.AUTO_TRADING:
                self._start_auto(stock)
            self.broadcast_status(stock.code)

    def apply_market_close(self) -> None:
        for stock in self.stocks.values():
            stock.machine.on_market_close()
            stock.engine = None
            self.broadcast_status(stock.code)

    def apply_market_reset(self) -> None:
        for stock in self.stocks.values():
            stock.machine.state = TradeState.MANUAL_TRADING
            stock.engine = None
            self.broadcast_status(stock.code)

    def _start_auto(self, stock: Stock) -> None:
        """MONITOR → AUTO_TRADING 진입 시 ULC 엔진 셋업."""
        bars = self.data.get_bars(stock.code, 3)
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

    # -- 라이프사이클 -----------------------------------------------------
    def start(self) -> None:
        """live 계좌면 미체결 폴링 루프 기동."""
        if self.live and self._orders_task is None:
            self._orders_task = asyncio.create_task(self._orders_loop())


class AccountManager:
    """여러 계좌 Hub를 총괄. 시계와 WS 버스를 공유."""

    def __init__(self) -> None:
        self.configs = load_account_configs()
        self.live = any(c.provider == "kiwoom" for c in self.configs)
        # live: 실제 KST 시계 자동 / mock-only: 수동 토글
        self.clock = MarketClock(auto=self.live)
        self._clients: Set[asyncio.Queue] = set()
        self._clock_task: Optional[asyncio.Task] = None
        self.hubs: Dict[str, Hub] = {}
        for cfg in self.configs:
            self.hubs[cfg.id] = Hub(cfg, self.clock, self)
        logger.info("계좌 활성화: %s", ", ".join(f"{c.id}({c.label})" for c in self.configs))

    # -- WebSocket pub/sub -------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
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
        """계좌에 속하지 않는 전역 로그(장 이벤트 등)."""
        self.broadcast({"type": "log", "text": text})

    # -- 조회 --------------------------------------------------------------
    def get(self, account: str) -> Optional[Hub]:
        return self.hubs.get(account)

    def accounts_payload(self) -> List[dict]:
        return [
            {"id": c.id, "label": c.label,
             "live": c.provider == "kiwoom", "danger": c.danger}
            for c in self.configs
        ]

    # -- 라이프사이클 -----------------------------------------------------
    async def restore(self) -> None:
        for hub in self.hubs.values():
            await hub.restore()

    def start(self) -> None:
        for hub in self.hubs.values():
            hub.start()
        self.start_clock()

    # -- 장 이벤트 (공유 시계) ---------------------------------------------
    def market_open(self) -> None:
        self.clock.open()
        self._log("📈 장 시작 (MARKET-OPEN)")
        for hub in self.hubs.values():
            hub.apply_market_open()
        self.broadcast_market()

    def market_close(self) -> None:
        self.clock.reset()
        self._log("📉 장 종료 (MARKET-CLOSE) → 장전·수동매매 초기 상태로 리셋")
        for hub in self.hubs.values():
            hub.apply_market_close()
        self.broadcast_market()

    def market_reset(self) -> None:
        self.clock.reset()
        for hub in self.hubs.values():
            hub.apply_market_reset()
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
        if not self.live:
            return
        if self._clock_task is None:
            self._clock_task = asyncio.create_task(self._auto_clock_loop())

    async def _auto_clock_loop(self) -> None:
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
                    self.market_open()
                elif last == MarketPhase.OPEN and cur == MarketPhase.CLOSED:
                    self.market_close()
                else:
                    self.broadcast_market()
                last = cur
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                self._log(f"⚠️ 장 시계 오류: {e}")


manager = AccountManager()
