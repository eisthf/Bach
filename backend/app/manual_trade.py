"""계좌별 수동 매매 관리자.

어떤 자동매매(ULC [상한가 매매], 대금 양봉, 구간)에서든 사람이 넘겨받은 종목과, 처음부터 사람이
직접 사고파는 종목을 한 목록에서 관리한다. 자동매매 화면에는 수동 매매가 없다 — 전환하면
종목이 원래 목록에서 빠지고 이 목록으로 옮겨진다(되돌리기는 없다: 보유 0이 되면 지우고
해당 화면에 새로 추가한다).

- 엔진이 없다. 보유·평단은 계좌 잔고(Broker.position)가 진실원이다. 그래서 전환 때 남아 있던
  엔진의 미체결 주문이 나중에 체결돼도 계산이 어긋나지 않는다(화면의 미체결 목록으로 보인다).
- 상태머신·MARKET-CLOSE와 무관하고, 재시작하면 목록을 그대로 복원한다. 계좌 보유 중 어느
  목록에도 없는 종목(HTS·모바일 매수 등)도 재시작·[보유 종목 가져오기] 때 이리로 들어온다.
- 자동 손절·익절(``ExitRule``)을 종목별로 켤 수 있다. 각각 '평단 대비 %' 또는 '가격 라인' 방식:
    손절  pct: 현재가 ≤ 평단 × (1 − x%)   price: 현재가 < 손절 라인
    익절  pct: 현재가 ≥ 평단 × (1 + y%)   price: 현재가 ≥ 익절 라인
  정규장 틱에서만 판단하고, 조건이 되면 보유 전량을 시장가로 한 번 판다. 주문을 보내면 손절·익절
  **둘 다** 꺼진다(재시작·미체결 중 다시 팔지 않게). 비율 방식의 기준 평단은 계좌 평단이라 수동으로
  더 사면 선도 따라 움직인다.
- 한 종목은 한 방식으로만(``occupied``) — 다른 목록과 같은 종목을 함께 두지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import aclosing
from dataclasses import dataclass, field
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
    "bonus": "무상증자 매매",
}


EXIT_MODES = ("pct", "price")


@dataclass
class ExitRule:
    """자동 손절 또는 익절 한 가지. mode="pct"면 평단 대비 %, "price"면 가격 라인."""
    enabled: bool = False
    mode: str = "pct"
    pct: float = 5.0
    price: float = 0.0

    def level(self, avg: float, side: str) -> Optional[float]:
        """발동 가격. 비율 방식은 평단이 있어야 정해진다."""
        if self.mode == "price":
            return self.price if self.price > 0 else None
        if avg <= 0:
            return None
        return avg * (1 - self.pct / 100) if side == "stop" else avg * (1 + self.pct / 100)

    def hit(self, price: float, avg: float, side: str) -> Optional[float]:
        """현재가가 조건을 넘었으면 발동 가격, 아니면 None. 손절 라인은 '미만', 나머지는 경계 포함."""
        level = self.level(avg, side)
        if not self.enabled or level is None:
            return None
        if side == "take_profit":
            return level if price >= level else None
        if self.mode == "price":
            return level if price < level else None
        return level if price <= level else None

    def to_dict(self) -> dict:
        return {"enabled": self.enabled, "mode": self.mode, "pct": self.pct, "price": self.price}

    @classmethod
    def parse(cls, data: Optional[dict], side: str, default_pct: float) -> "ExitRule":
        """요청·저장값을 검사해 만든다. 잘못된 값이면 ValueError."""
        data = data or {}
        name = "손절" if side == "stop" else "익절"
        mode = str(data.get("mode") or "pct")
        if mode not in EXIT_MODES:
            raise ValueError(f"{name} 방식은 pct 또는 price 여야 합니다.")
        try:
            pct = float(data.get("pct", default_pct))
            price = float(data.get("price") or 0)
        except (TypeError, ValueError):
            raise ValueError(f"{name} 값은 숫자여야 합니다.") from None
        enabled = bool(data.get("enabled"))
        high = 100 if side == "stop" else 1000
        if not (0 < pct < high) and (enabled and mode == "pct"):
            raise ValueError(f"{name} 비율은 0 초과 {high}% 미만이어야 합니다.")
        if enabled and mode == "price" and price <= 0:
            raise ValueError(f"{name} 라인 가격을 정하세요.")
        return cls(enabled=enabled, mode=mode, pct=pct if 0 < pct < high else default_pct,
                   price=max(0.0, price))


@dataclass
class ManualItem:
    code: str
    name: str
    origin: str = "added"
    notice: str = ""
    created_at: str = ""
    stop: ExitRule = field(default_factory=lambda: ExitRule(pct=5.0))
    take_profit: ExitRule = field(default_factory=lambda: ExitRule(pct=10.0))
    task: Optional[asyncio.Task] = None
    exit_triggered: bool = False
    exit_checked_at: float = 0.0


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
            "exits": {
                side: {**rule.to_dict(),
                       "level": rule.level(pos.avg_price if verified else 0.0, side)}
                for side, rule in (("stop", it.stop), ("take_profit", it.take_profit))
            },
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
             "created_at": it.created_at,
             "stop": it.stop.to_dict(), "take_profit": it.take_profit.to_dict()}
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
                if "stop" not in entry and "stop_enabled" in entry:
                    # 이전 저장 형식: 평단 대비 자동 손절만 있었다.
                    entry = {**entry, "stop": {"enabled": entry.get("stop_enabled"), "mode": "pct",
                                               "pct": entry.get("stop_pct", 5.0)}}
                it = ManualItem(code=code, name=entry.get("name") or code,
                                origin=entry.get("origin") or "added",
                                notice=entry.get("notice", ""),
                                created_at=entry.get("created_at", ""),
                                stop=self._load_rule(code, entry.get("stop"), "stop", 5.0),
                                take_profit=self._load_rule(code, entry.get("take_profit"),
                                                            "take_profit", 10.0))
                self.items[code] = it
                self._start_loop(it)
        finally:
            self._restoring = False

    def _load_rule(self, code: str, data, side: str, default_pct: float) -> ExitRule:
        try:
            return ExitRule.parse(data, side, default_pct)
        except ValueError:
            self.hub._log(f"[{code}] ⚠️ 저장된 자동 {'손절' if side == 'stop' else '익절'} 설정 오류 — 끔")
            return ExitRule(pct=default_pct)

    # -- 등록·제거 -----------------------------------------------------------
    def add(self, code: str, name: str = "", origin: str = "added", notice: str = "",
            stop_enabled: bool = False, stop_pct: float = 5.0,
            exits: Optional[tuple] = None) -> dict:
        """목록에 올린다. 이미 있으면 그대로(알림만 갱신). 다른 목록이 쓰는 종목은 거부.

        ``exits`` = (손절 ExitRule, 익절 ExitRule) — 처음부터 손절·익절을 정해 올릴 때(무상증자 매매).
        """
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
                        stop=ExitRule(enabled=stop_enabled, mode="pct",
                                      pct=stop_pct if 0 < stop_pct < 100 else 5.0))
        if exits is not None:
            it.stop, it.take_profit = exits
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

    def set_exits(self, code: str, stop: Optional[dict], take_profit: Optional[dict]) -> dict:
        """자동 손절·익절 설정. 둘 다 켜져 있고 발동 가격을 알 수 있으면 손절 < 익절이어야 한다."""
        it = self._get(code)
        new_stop = ExitRule.parse(stop, "stop", 5.0)
        new_tp = ExitRule.parse(take_profit, "take_profit", 10.0)
        if new_stop.enabled and new_tp.enabled:
            try:
                avg = self.hub.broker.position(code).avg_price
            except Exception:  # noqa: BLE001
                avg = 0.0
            lo, hi = new_stop.level(avg, "stop"), new_tp.level(avg, "take_profit")
            if lo is not None and hi is not None and lo >= hi:
                raise ValueError(f"손절 가격({lo:,.0f})은 익절 가격({hi:,.0f})보다 낮아야 합니다.")
        it.stop, it.take_profit = new_stop, new_tp
        it.exit_triggered = False
        it.exit_checked_at = 0.0
        self.persist()
        self._log(code, f"자동 청산 설정 — {self.describe(it)}")
        self.broadcast(code)
        return self.payload(code)

    @staticmethod
    def describe(it: ManualItem) -> str:
        def one(rule: ExitRule, side: str) -> str:
            name = "손절" if side == "stop" else "익절"
            if not rule.enabled:
                return f"{name} 끔"
            if rule.mode == "price":
                return f"{name} {rule.price:,.0f}원 {'미만' if side == 'stop' else '이상'}"
            return f"{name} 평단 {'−' if side == 'stop' else '+'}{rule.pct:g}%"
        return f"{one(it.stop, 'stop')}, {one(it.take_profit, 'take_profit')}"

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
                    await self._run_exits(it, tick)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("[%s] 수동 매매 틱 루프 오류", it.code)
            self._log(it.code, "⚠️ 틱 처리 오류 — 로그 확인 필요")

    async def _run_exits(self, it: ManualItem, tick: Tick) -> None:
        """자동 손절·익절. 조건 충족 시 계좌 보유수량 전량을 한 번만 주문."""
        if (not (it.stop.enabled or it.take_profit.enabled) or it.exit_triggered
                or not self.hub.clock.is_open() or tick.price <= 0):
            return
        now = time.monotonic()
        if now - it.exit_checked_at < self.STOP_CHECK_SEC:
            return
        it.exit_checked_at = now
        try:
            pos = await asyncio.to_thread(self.hub.broker.position, it.code)
        except Exception as exc:  # noqa: BLE001
            self._log(it.code, f"자동 청산 잔고 조회 실패: {type(exc).__name__}",
                      event="manual_stop_error", stage="position", error_type=type(exc).__name__)
            return
        if pos.quantity <= 0:
            return
        side, level = "stop", it.stop.hit(tick.price, pos.avg_price, "stop")
        if level is None:
            side, level = "take_profit", it.take_profit.hit(tick.price, pos.avg_price, "take_profit")
        if level is None:
            return
        name = "손절" if side == "stop" else "익절"
        rule = it.stop if side == "stop" else it.take_profit
        basis = (f"{'손절' if side == 'stop' else '익절'} 라인 {level:,.0f}" if rule.mode == "price"
                 else f"평단 {pos.avg_price:,.0f} {'−' if side == 'stop' else '+'}{rule.pct:g}% = {level:,.0f}")

        # 주문 호출 동안 다음 틱이 중복 주문을 내지 못하게 먼저 잠근다.
        it.exit_triggered = True
        try:
            result = await asyncio.to_thread(self.hub.broker.sell, it.code, pos.quantity)
            self.hub.tag_order(result, "manual", f"수동 자동{name}",
                               {"exit": rule.to_dict(), "level": level, "avg": pos.avg_price})
        except Exception as exc:  # noqa: BLE001
            it.exit_triggered = False
            self._log(it.code, f"⚠️ 자동 {name} 주문 오류 — 다음 틱에 재시도",
                      event="manual_stop_error", stage="sell", price=tick.price,
                      stop_price=level, quantity=pos.quantity, error_type=type(exc).__name__)
            return
        if not result.ok:
            it.exit_triggered = False
            self._log(it.code, f"⚠️ 자동 {name} 주문 실패 — 다음 틱에 재시도",
                      event="manual_stop_error", stage="sell", price=tick.price,
                      stop_price=level, quantity=pos.quantity)
            return

        # 시장가 주문이 미체결인 동안 재시작해도 같은 수량을 다시 팔지 않도록
        # 손절·익절을 둘 다 끈다(일회성).
        it.stop.enabled = False
        it.take_profit.enabled = False
        self.hub.broker.invalidate()
        self.persist()
        self._log(it.code,
                  f"자동 {name}: {pos.quantity}주 시장가 매도 전송 (현재가 {tick.price:,.0f}, {basis}) "
                  "— 자동 손절·익절 해제",
                  event="manual_stop_triggered", side=side, price=tick.price, stop_price=level,
                  avg_price=pos.avg_price, mode=rule.mode, pct=rule.pct, quantity=pos.quantity)
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
