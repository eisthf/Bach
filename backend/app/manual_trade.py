"""계좌별 수동 매매 관리자.

어떤 자동매매(ULC [상한가 매매], 대금 양봉, 구간)에서든 사람이 넘겨받은 종목과, 처음부터 사람이
직접 사고파는 종목을 한 목록에서 관리한다. 자동매매 화면에는 수동 매매가 없다 — 전환하면
종목이 원래 목록에서 빠지고 이 목록으로 옮겨진다(되돌리기는 없다: 보유 0이 되면 지우고
해당 화면에 새로 추가한다).

- 엔진이 없다. 보유·평단은 계좌 잔고(Broker.position)가 진실원이다. 그래서 전환 때 남아 있던
  엔진의 미체결 주문이 나중에 체결돼도 계산이 어긋나지 않는다(화면의 미체결 목록으로 보인다).
- 상태머신·MARKET-CLOSE와 무관하고, 재시작하면 목록을 그대로 복원한다. 계좌 보유 중 어느
  목록에도 없는 종목(HTS·모바일 매수 등)도 재시작·[보유 종목 가져오기] 때 이리로 들어온다.
- 보호용 '평단 대비 자동 손절'(전량 시장가, 주문 전송 후 자동 해제)을 종목별로 켤 수 있다.
- 한 종목은 한 방식으로만(``occupied``) — 다른 목록과 같은 종목을 함께 두지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from .market_clock import now_kst
from .models import Position, Tick

if TYPE_CHECKING:
    from .hub import Hub

logger = logging.getLogger("bach.manual_trade")

# 어디서 넘어왔는가(화면 배지·로그용).
ORIGINS = {
    "added": "직접 추가",
    "ulc": "상한가 매매",
    "candle": "대금 양봉 매매",
    "zone": "구간 매매",
    "restore": "재시작 잔고",
    "import": "보유 종목 가져오기",
}


@dataclass
class ManualItem:
    code: str
    name: str
    origin: str = "added"
    notice: str = ""
    created_at: str = ""
    stop_enabled: bool = False
    stop_pct: float = 5.0
    task: Optional[asyncio.Task] = None
    stop_triggered: bool = False
    stop_checked_at: float = 0.0


class ManualTradeManager:
    # 고빈도 틱마다 계좌 잔고를 조회하지 않는다(KiwoomBroker 2초 캐시와 별개로 한 번 더 줄인다).
    STOP_CHECK_SEC = 1.0

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.items: Dict[str, ManualItem] = {}
        self._path: Path = hub._state_path.with_suffix(".manual.json")
        self._restoring = False

    # -- 조회 ----------------------------------------------------------------
    def occupied(self) -> set[str]:
        return set(self.items)

    def payload(self, code: str) -> Optional[dict]:
        it = self.items.get(code)
        if it is None:
            return None
        verified = True
        try:
            pos = self.hub.broker.position(code)
        except Exception:  # noqa: BLE001
            pos = Position(code=code)
            verified = False
        return {
            "code": code, "name": it.name, "origin": it.origin, "notice": it.notice,
            "created_at": it.created_at,
            "stop": {"enabled": it.stop_enabled, "pct": it.stop_pct},
            "position": pos.model_dump(), "position_verified": verified,
        }

    def list_payload(self) -> List[dict]:
        return [p for p in (self.payload(c) for c in list(self.items)) if p is not None]

    def broadcast(self, code: str) -> None:
        p = self.payload(code)
        if p is not None:
            self.hub.broadcast({"type": "manual_trade", "item": p})

    async def broadcast_async(self, code: str) -> None:
        # payload는 계좌 조회(블로킹 HTTP)를 탈 수 있어 스레드에서 만든다.
        p = await asyncio.to_thread(self.payload, code)
        if p is not None:
            self.hub.broadcast({"type": "manual_trade", "item": p})

    def _log(self, code: str, text: str, **fields) -> None:
        self.hub._log(f"[{code}] 수동매매: {text}", **({"event": "manual_trade", "code": code} | fields))

    # -- 영속화 --------------------------------------------------------------
    def persist(self) -> None:
        if self._restoring:
            return
        data = {"items": [
            {"code": it.code, "name": it.name, "origin": it.origin, "notice": it.notice,
             "created_at": it.created_at, "stop_enabled": it.stop_enabled, "stop_pct": it.stop_pct}
            for it in self.items.values()
        ]}
        try:
            self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.warning("수동 매매 상태 저장 실패(%s): %s", self._path, e)

    def load_saved(self) -> List[dict]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception as e:  # noqa: BLE001
            logger.warning("수동 매매 상태 로드 실패(%s): %s", self._path, e)
            self.hub._log(f"⚠️ 수동 매매 상태 파일을 읽지 못했습니다: {self._path.name}")
            return []
        return [e for e in raw.get("items") or [] if str(e.get("code") or "").strip()]

    def restore(self, saved: List[dict]) -> None:
        """저장된 목록 복원(재시작). 계좌 잔고 끌어오기는 Hub.restore가 맡는다."""
        self._restoring = True
        try:
            for entry in saved:
                code = str(entry["code"]).strip()
                if code in self.items:
                    continue
                try:
                    pct = float(entry.get("stop_pct", 5.0))
                except (TypeError, ValueError):
                    pct = 5.0
                it = ManualItem(code=code, name=entry.get("name") or code,
                                origin=entry.get("origin") or "added",
                                notice=entry.get("notice", ""),
                                created_at=entry.get("created_at", ""),
                                stop_enabled=bool(entry.get("stop_enabled")), stop_pct=pct)
                self.items[code] = it
                self._start_loop(it)
        finally:
            self._restoring = False

    # -- 등록·제거 -----------------------------------------------------------
    def add(self, code: str, name: str = "", origin: str = "added", notice: str = "",
            stop_enabled: bool = False, stop_pct: float = 5.0) -> dict:
        """목록에 올린다. 이미 있으면 그대로(알림만 갱신). 다른 목록이 쓰는 종목은 거부."""
        existing = self.items.get(code)
        if existing is not None:
            if notice:
                existing.notice = notice
                self.persist()
                self.broadcast(code)
            return self.payload(code)
        if code in self.hub.stocks:
            raise ValueError("[상한가 매매] 목록에 있는 종목입니다. 그 화면에서 [수동 전환]하세요.")
        if code in self.hub.candle_trades.occupied():
            raise ValueError("대금 양봉·구간 매매에서 운용 중인 종목입니다. 그 화면에서 [수동 전환]하세요.")
        it = ManualItem(code=code, name=name or code, origin=origin, notice=notice,
                        created_at=now_kst().isoformat(timespec="seconds"),
                        stop_enabled=stop_enabled, stop_pct=stop_pct)
        self.items[code] = it
        self._start_loop(it)
        self.persist()
        if not self._restoring:
            self._log(code, f"목록에 추가({ORIGINS.get(origin, origin)})"
                      + (f" — {notice}" if notice else ""), origin=origin)
            self.broadcast(code)
            lt = self.hub.data.last_tick(code)
            if lt is not None:
                self.hub.broadcast({"type": "tick", "tick": lt.model_dump()})
        return self.payload(code)

    async def remove(self, code: str) -> None:
        """목록에서 지운다. 계좌에 보유가 남았으면 거부(관리하지 않는 보유가 생기지 않게)."""
        it = self._get(code)
        self.hub.broker.invalidate()
        try:
            pos = await asyncio.to_thread(self.hub.broker.position, code)
        except Exception:  # noqa: BLE001
            raise ValueError("계좌 잔고를 확인하지 못해 지울 수 없습니다. 잠시 후 다시 시도하세요.")
        if pos.quantity > 0:
            raise ValueError(f"계좌에 {pos.quantity}주를 보유 중입니다. 전량 매도한 뒤 지우세요.")
        await self._stop_loop(it)
        self.items.pop(code, None)
        self.persist()
        self._log(code, "목록에서 지움")
        self.hub.broadcast({"type": "manual_trade_removed", "code": code})

    def set_stop(self, code: str, enabled: bool, pct: float) -> dict:
        if not (0 < pct <= 100):
            raise ValueError("손절률은 0 초과 100% 이하여야 합니다.")
        it = self._get(code)
        it.stop_enabled, it.stop_pct = bool(enabled), float(pct)
        it.stop_triggered = False
        it.stop_checked_at = 0.0
        self.persist()
        self.broadcast(code)
        return self.payload(code)

    def clear_notice(self, code: str) -> dict:
        it = self._get(code)
        it.notice = ""
        self.persist()
        self.broadcast(code)
        return self.payload(code)

    # -- 틱·체결 -------------------------------------------------------------
    def _start_loop(self, it: ManualItem) -> None:
        if it.task is None or it.task.done():
            it.task = asyncio.create_task(self._tick_loop(it))

    async def _stop_loop(self, it: ManualItem) -> None:
        task, it.task = it.task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _tick_loop(self, it: ManualItem) -> None:
        try:
            # aclosing: 지울 때 구독 해제(finally)가 즉시 돌게 한다(시세 구독은 종목당 하나).
            async with aclosing(self.hub.data.stream_ticks(it.code)) as ticks:
                async for tick in ticks:
                    self.hub.broadcast({"type": "tick", "tick": tick.model_dump()})
                    await self._run_stop(it, tick)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("[%s] 수동 매매 틱 루프 오류", it.code)
            self._log(it.code, "⚠️ 틱 처리 오류 — 로그 확인 필요")

    async def _run_stop(self, it: ManualItem, tick: Tick) -> None:
        """평단 대비 자동 손절. 조건 충족 시 계좌 보유수량 전량을 한 번만 주문."""
        if (not it.stop_enabled or it.stop_triggered
                or not self.hub.clock.is_open() or tick.price <= 0):
            return
        now = time.monotonic()
        if now - it.stop_checked_at < self.STOP_CHECK_SEC:
            return
        it.stop_checked_at = now
        try:
            pos = await asyncio.to_thread(self.hub.broker.position, it.code)
        except Exception as exc:  # noqa: BLE001
            self._log(it.code, f"자동 손절 잔고 조회 실패: {type(exc).__name__}",
                      event="manual_stop_error", stage="position", error_type=type(exc).__name__)
            return
        if pos.quantity <= 0 or pos.avg_price <= 0:
            return
        stop_price = pos.avg_price * (1 - it.stop_pct / 100)
        if tick.price > stop_price:
            return

        # 주문 호출 동안 다음 틱이 중복 주문을 내지 못하게 먼저 잠근다.
        it.stop_triggered = True
        try:
            result = await asyncio.to_thread(self.hub.broker.sell, it.code, pos.quantity)
            self.hub.tag_order(result, "manual", "수동 자동손절")
        except Exception as exc:  # noqa: BLE001
            it.stop_triggered = False
            self._log(it.code, "⚠️ 자동 손절 주문 오류 — 다음 틱에 재시도",
                      event="manual_stop_error", stage="sell", price=tick.price,
                      stop_price=stop_price, quantity=pos.quantity, error_type=type(exc).__name__)
            return
        if not result.ok:
            it.stop_triggered = False
            self._log(it.code, "⚠️ 자동 손절 주문 실패 — 다음 틱에 재시도",
                      event="manual_stop_error", stage="sell", price=tick.price,
                      stop_price=stop_price, quantity=pos.quantity)
            return

        # 시장가 주문이 미체결인 동안 재시작해도 같은 수량을 다시 팔지 않도록
        # 성공적으로 전송한 보호 옵션은 일회성으로 해제한다.
        it.stop_enabled = False
        self.hub.broker.invalidate()
        self.persist()
        self._log(it.code,
                  f"자동 손절: {pos.quantity}주 시장가 매도 전송 (현재가 {tick.price:,.0f} ≤ "
                  f"기준선 {stop_price:,.0f}, 평단 {pos.avg_price:,.0f}, 손절률 {it.stop_pct:g}%)",
                  event="manual_stop_triggered", price=tick.price, stop_price=stop_price,
                  avg_price=pos.avg_price, stop_pct=it.stop_pct, quantity=pos.quantity)
        await self.broadcast_async(it.code)
        await self.hub.refresh_orders()

    def on_fill(self, fill: dict) -> None:
        code = fill.get("code")
        if code not in self.items:
            return
        side = "매수" if fill.get("side") == "buy" else "매도"
        self._log(code, f"체결: {side} {fill.get('qty')}주 @ {fill.get('price', 0):,.0f} "
                        f"(미체결 {fill.get('unfilled')})")
        loop = asyncio.get_running_loop()
        loop.create_task(self.broadcast_async(code))
        loop.create_task(self.hub.refresh_orders())

    async def refresh_all(self) -> None:
        for code in list(self.items):
            await self.broadcast_async(code)

    # -- 기타 ----------------------------------------------------------------
    def _get(self, code: str) -> ManualItem:
        it = self.items.get(code)
        if it is None:
            raise KeyError(code)
        return it

    async def close(self) -> None:
        for it in list(self.items.values()):
            await self._stop_loop(it)
