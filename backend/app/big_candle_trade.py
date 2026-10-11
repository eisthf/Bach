"""계좌별 대금 양봉 매매 관리자.

기존 매매(Hub.stocks + 상태머신)와 **분리된** 목록이다. 이유:
- 여러 거래일에 걸쳐 감시·보유하므로 MARKET-CLOSE 초기화 대상이 아니다.
- 재시작하면 기존 매매는 전부 수동 인계되지만, 대금 양봉 매매는 저장된 단계부터
  이어서 감시해야 한다(단, 계좌 잔고로 대조한 뒤에만 주문을 재개한다).

전략 엔진(``strategy/big_candle_trade.py``)이 모르는 하루치 맥락 — 전일 종가와
기준일 이후 경과 거래일 — 은 여기서 세션마다 일봉을 조회해 넣는다. 같은 조회로
기준일 일봉도 확인한다(장중 스냅샷으로 등록했다면 시가·종가가 확정값과 다르다).

같은 계좌에서 같은 종목을 두 방식이 동시에 다루지 않는다(``occupied``).
포지션 진실원이 계좌 잔고 하나라, 두 엔진이 같은 수량을 서로 제 것으로
판단하면 이중 매도·과다 매도가 날 수 있기 때문이다. 수동 인계 시 종목은
기존 [매매] 목록으로 옮겨져(MANUAL_TRADING) 수동 매도·자동손절을 그대로 쓴다.

주문·체결·시세는 Hub의 provider/broker를 그대로 쓴다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional

from .market_clock import now_kst
from .models import CandleTradeConfig, Tick
from .providers.base import DAY_INTERVAL
from .strategy.big_candle_trade import CandleTradeEngine, CzPhase

if TYPE_CHECKING:
    from .hub import Hub

logger = logging.getLogger("bach.big_candle_trade")


def _bar_date(epoch: int) -> str:
    """봉 시각(KST 벽시계를 UTC처럼 환산한 epoch) → YYYY-MM-DD."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).date().isoformat()


@dataclass
class CandleItem:
    code: str
    name: str
    engine: CandleTradeEngine
    task: Optional[asyncio.Task] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 계좌 잔고로 엔진 수량을 확인했는가. 확인 전엔 주문하지 않는다(재시작 직후).
    verified: bool = True
    verify_checked_at: float = 0.0
    # 1차 매수를 ULC 1차에 양보하기 시작한 시각(monotonic). 0이면 양보한 적 없음.
    yield_started_at: float = 0.0
    # 하루 맥락(일봉) 조회를 마지막으로 시도한 시각(monotonic). 실패 시 재시도 간격용.
    day_checked_at: float = 0.0
    notice: str = ""
    created_at: str = ""


class CandleTradeManager:
    VERIFY_RETRY_SEC = 10.0
    DAY_RETRY_SEC = 10.0
    # 경과 거래일·전일 종가 계산용 일봉 조회량. 기한(entry_days) 최대 60거래일 + 여유.
    DAY_LOOKBACK = 90
    # 09:00 직후 상한가 매매(ULC) 1차 주문에 길을 내주는 최대 대기.
    # REST 호출은 0.35초 간격 한 줄로 나가므로, 1차 매수가 먼저 끼어들면
    # 그만큼 ULC 1차가 밀린다. ULC는 시가 직후 몇 초가 전부라 우선권을 준다.
    # 다만 ULC 셋업이 지연돼도 대금 양봉 매매가 영영 막히지 않도록 상한을 둔다.
    ULC_YIELD_MAX_SEC = 90.0

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.items: Dict[str, CandleItem] = {}
        self._path: Path = hub._state_path.with_suffix(".candle.json")
        self._restoring = False

    # -- 조회 ----------------------------------------------------------------
    def occupied(self) -> set[str]:
        """기존 매매 목록에 올릴 수 없는 종목(대금 양봉 매매가 설정 중이거나 운용 중)."""
        return {c for c, it in self.items.items()
                if it.engine.phase == CzPhase.DRAFT or it.engine.phase.active}

    def payload(self, code: str) -> Optional[dict]:
        it = self.items.get(code)
        if it is None:
            return None
        eng = it.engine
        legs = []
        for i, leg in enumerate(eng.legs):
            legs.append({
                "no": i + 1, "ratio": leg.ratio, "amount": leg.amount, "status": leg.status,
                "qty": leg.qty, "price": leg.price, "confirmed": leg.fill_qty > 0,
                "date": leg.date, "trigger": eng.add_trigger() if i == 1 else None,
            })
        zone = eng.zone()
        return {
            "code": code, "name": it.name, "phase": eng.phase.value,
            "config": eng.config.model_dump(), "legs": legs,
            "base": {"date": eng.base_date, "open": eng.base_open, "close": eng.base_close,
                     "confirmed": eng.base_confirmed},
            "zone": {"low": zone[0], "high": zone[1]} if zone else None,
            "day": eng.day, "days_elapsed": eng.days_elapsed, "prev_close": eng.prev_close,
            "stop_armed": eng.stop_armed, "peak": eng.peak,
            "take_profit_price": eng.take_profit_price(), "trailing_stop": eng.trailing_stop(),
            "shares": eng.shares, "sold_qty": eng.sold_qty, "avg_cost": eng.avg_cost,
            "exit_reason": eng.exit_reason,
            "verified": it.verified, "notice": it.notice, "created_at": it.created_at,
        }

    def list_payload(self) -> list[dict]:
        return [self.payload(c) for c in self.items]

    def broadcast(self, code: str) -> None:
        p = self.payload(code)
        if p is not None:
            self.hub.broadcast({"type": "candle_trade", "item": p})

    def _log(self, code: str, text: str) -> None:
        self.hub._log(f"[{code}] 대금양봉: {text}", event="candle_trade", code=code)

    # -- 영속화 --------------------------------------------------------------
    def persist(self) -> None:
        if self._restoring:
            return
        data = {"items": [
            {"code": it.code, "name": it.name, "config": it.engine.config.model_dump(),
             "engine": it.engine.to_dict(), "notice": it.notice, "created_at": it.created_at}
            for it in self.items.values()
        ]}
        try:
            self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.warning("대금 양봉 매매 상태 저장 실패(%s): %s", self._path, e)

    async def restore(self, positions: Optional[dict]) -> None:
        """저장된 대금 양봉 매매를 복원한다. ``positions``는 {code: Position} 또는
        None(잔고 조회 실패). 조회 실패면 확인될 때까지 주문을 보류한다."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            legacy = self.hub._state_path.with_suffix(".close.json")
            if legacy.exists():
                # 이전 '종가 매매'(다른 전략) 기록은 옮기지 않는다. 그 종목의 계좌 잔고는
                # 점유되지 않았으므로 재시작 잔고 가져오기로 [매매] 목록에 수동매매로 올라온다.
                self.hub._log(f"⚠️ 이전 종가 매매 기록({legacy.name})은 대금 양봉 매매로 옮기지 않았습니다 — "
                              "보유 종목은 [매매] 목록에서 확인하세요.")
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("대금 양봉 매매 상태 로드 실패(%s): %s", self._path, e)
            self.hub._log(f"⚠️ 대금 양봉 매매 상태 파일을 읽지 못했습니다: {self._path.name}")
            return
        self._restoring = True
        try:
            for entry in raw.get("items") or []:
                code = str(entry.get("code") or "").strip()
                if not code:
                    continue
                try:
                    cfg = CandleTradeConfig(**(entry.get("config") or {}))
                    eng = CandleTradeEngine.from_dict(code, cfg, entry.get("engine") or {},
                                                     log=self._engine_log(code))
                except Exception:  # noqa: BLE001
                    logger.exception("[%s] 대금 양봉 매매 복원 실패", code)
                    self.hub._log(f"[{code}] ⚠️ 대금 양봉 매매 저장 상태 오류 — 복원하지 않음(계좌에서 확인 필요)")
                    continue
                it = CandleItem(code=code, name=entry.get("name") or code, engine=eng,
                               notice=entry.get("notice", ""),
                               created_at=entry.get("created_at", ""))
                self.items[code] = it
                if eng.phase.active and eng.shares > 0:
                    if positions is None:
                        it.verified = False
                        it.notice = "재시작 잔고 확인 실패 — 확인될 때까지 주문 보류"
                    else:
                        pos = positions.get(code)
                        self._reconcile(it, pos.quantity if pos else 0)
                if eng.phase.active:
                    self._start_loop(it)
                self._log(code, f"복원: {eng.phase.value}"
                          + (f", 보유 {eng.shares}주 평단 {eng.avg_cost:,.0f}" if eng.shares else ""))
        finally:
            self._restoring = False
        self.persist()

    def _reconcile(self, it: CandleItem, account_qty: int) -> None:
        """계좌 잔고로 엔진 수량을 확인한다(보수적)."""
        eng = it.engine
        it.verified = True
        if account_qty <= 0:
            eng.phase = CzPhase.DONE
            eng.exit_reason = "계좌 잔고 0주(외부 청산 추정)"
            it.notice = f"계좌에 보유 수량이 없어 종료했습니다(엔진 기록 {eng.shares}주)."
            self._log(it.code, f"⚠️ {it.notice}")
        elif account_qty < eng.shares:
            # 엔진이 제 몫보다 많이 팔려 들 수 있다 → 자동 운용을 멈추고 사람에게.
            it.notice = f"계좌 잔고 {account_qty}주 < 엔진 기록 {eng.shares}주 — 수동 인계"
            eng.hand_off(it.notice)
            self._log(it.code, f"⚠️ {it.notice}")
            asyncio.get_running_loop().create_task(self._move_to_manual(it))
        elif it.notice.startswith("재시작 잔고 확인 실패"):
            it.notice = ""

    # -- 예수금 가드 / 주문 우선순위 -------------------------------------------
    def committed_krw(self, exclude: str = "") -> int:
        """이 계좌에서 이미 '쓰기로 되어 있는' 금액.

        - 다른 대금 양봉 매매의 남은 차수 금액(아직 안 산 몫)
        - [매매]에서 오늘 자동매매가 예정·진행 중인 종목(MONITOR/AUTO_TRADING)의 총 투자액
        설정 중(DRAFT)인 대금 양봉 매매는 아직 주문을 시작하지 않았으므로 제외한다.
        """
        from .models import TradeState

        total = 0
        for code, it in self.items.items():
            if code == exclude or not it.engine.phase.active:
                continue
            total += sum(leg.amount for leg in it.engine.legs if leg.status == "waiting")
        for stock in self.hub.stocks.values():
            if stock.machine.state in (TradeState.MONITOR, TradeState.AUTO_TRADING):
                total += int(stock.config.max_buy_amount)
        return total

    async def _budget_notice(self, it: CandleItem) -> str:
        """주문가능금액 확인. 1차 금액도 모자라면 예외, 계획 전체가 모자라면 경고 문구.

        주문가능금액을 못 얻는 환경(mock 등)에서는 검사를 건너뛴다 — 없는 값으로
        막으면 데모가 통째로 멈춘다.
        """
        try:
            summary = await asyncio.to_thread(self.hub.broker.account_summary)
        except Exception:  # noqa: BLE001
            return ""
        orderable = int((summary or {}).get("orderable") or 0)
        if orderable <= 0:
            return ""
        eng = it.engine
        committed = self.committed_krw(exclude=it.code)
        first = eng.legs[0].amount
        plan = sum(leg.amount for leg in eng.legs)
        if orderable < committed + first:
            raise ValueError(
                f"주문가능금액이 부족합니다. 가능 {orderable:,}원, "
                f"다른 전략 약정 {committed:,}원 + 1차 {first:,}원 필요."
            )
        if orderable < committed + plan:
            return (f"주문가능금액 {orderable:,}원 < 계획 {plan:,}원"
                    + (f" + 다른 전략 약정 {committed:,}원" if committed else "")
                    + " — 뒤 차수에서 잔고 부족으로 매수가 실패할 수 있습니다.")
        return ""

    def _ulc_first_orders_pending(self) -> bool:
        """[매매] 쪽에 아직 1차 주문을 내지 못한 자동매매 종목이 있는가."""
        from .models import TradeState

        for stock in self.hub.stocks.values():
            if stock.machine.state != TradeState.AUTO_TRADING:
                continue
            eng = stock.engine
            if eng is None:                       # 셋업(기준가 확인) 진행 중
                return True
            if not any(leg.filled for leg in getattr(eng, "legs", [])):
                return True
        return False

    def _should_yield_to_ulc(self, it: CandleItem) -> bool:
        """1차 매수(WATCHING)를 ULC 1차 주문 뒤로 미룰지."""
        if not self._ulc_first_orders_pending():
            it.yield_started_at = 0.0
            return False
        now = time.monotonic()
        if not it.yield_started_at:
            it.yield_started_at = now
            self._log(it.code, "1차 매수 대기: 상한가 매매 1차 주문을 먼저 보냅니다")
            return True
        return now - it.yield_started_at < self.ULC_YIELD_MAX_SEC

    # -- 등록·설정 -----------------------------------------------------------
    def add(self, code: str, name: str = "", base: Optional[dict] = None) -> dict:
        """등록(설정 중). ``base``는 기준 캔들 {date, open, close} — 대금 양봉 조회일의 봉."""
        if code in self.hub.stocks:
            raise ValueError("이미 [매매] 목록에 있는 종목입니다. 한 종목은 한 방식으로만 운용합니다.")
        base = base or {}
        date = str(base.get("date") or "").strip()
        try:
            open_, close = float(base.get("open") or 0), float(base.get("close") or 0)
        except (TypeError, ValueError):
            open_ = close = 0.0
        if not date or open_ <= 0 or close <= open_:
            raise ValueError("기준 캔들(조회일 양봉의 날짜·시가·종가)이 필요합니다.")
        existing = self.items.get(code)
        if existing is not None:
            if code in self.occupied():
                return self.payload(code)
            # 끝난 기록(DONE/MANUAL)은 새 등록으로 대체한다.
            self.items.pop(code)
        eng = CandleTradeEngine(code, CandleTradeConfig(), log=self._engine_log(code))
        eng.set_base(date, open_, close, confirmed=False)
        it = CandleItem(code=code, name=name or code, engine=eng,
                        created_at=now_kst().isoformat(timespec="seconds"))
        self.items[code] = it
        self.persist()
        self._log(code, f"등록(설정 중) — 기준 캔들 {date} 시가 {open_:,.0f} 종가 {close:,.0f}")
        self.broadcast(code)
        return self.payload(code)

    def set_config(self, code: str, config: CandleTradeConfig) -> dict:
        it = self._get(code)
        if not (it.engine.phase == CzPhase.DRAFT or it.engine.phase.active):
            raise ValueError("종료된 대금 양봉 매매는 설정을 바꿀 수 없습니다.")
        it.engine.reconfigure(config)
        self.persist()
        self.broadcast(code)
        return self.payload(code)

    async def remove(self, code: str) -> None:
        it = self._get(code)
        if it.engine.phase.active:
            raise ValueError("운용 중인 종목은 삭제할 수 없습니다. 먼저 감시 중지나 수동 전환을 하세요.")
        await self._stop_loop(it)
        self.items.pop(code, None)
        self.persist()
        self.hub.broadcast({"type": "candle_trade_removed", "code": code})

    # -- 시작·중지·인계 ------------------------------------------------------
    async def start(self, code: str) -> dict:
        """[감시 시작]. 주문은 매입 구간에 들어올 때 나간다."""
        it = self._get(code)
        async with it.lock:
            notice = await self._budget_notice(it)
            it.engine.start()
            it.notice = notice
            if notice:
                self._log(code, f"⚠️ {notice}")
            self._start_loop(it)
            self.persist()
        self.broadcast(code)
        return self.payload(code)

    async def stop(self, code: str) -> dict:
        """1차 매수 전 감시 중지(설정 중으로 되돌림)."""
        it = self._get(code)
        async with it.lock:
            if not it.engine.stop():
                raise ValueError("1차 매수 전 감시 중인 종목만 중지할 수 있습니다.")
            it.notice = ""
        await self._stop_loop(it)
        self.persist()
        self.broadcast(code)
        return self.payload(code)

    async def hand_off(self, code: str) -> dict:
        """수동 전환: 엔진을 멈추고 종목을 [매매] 목록(MANUAL_TRADING)으로 옮긴다."""
        it = self._get(code)
        async with it.lock:
            if not it.engine.phase.holding:
                raise ValueError("보유 중인 종목만 수동 전환할 수 있습니다.")
            it.engine.hand_off("사용자 수동 전환")
        await self._move_to_manual(it)
        return self.payload(code)

    async def _move_to_manual(self, it: CandleItem) -> None:
        # 시세 구독은 종목당 하나다. 대금 양봉 매매 루프를 완전히 끝낸 뒤(구독 해제 포함)
        # 기존 매매 목록에 올려야 새 구독이 덮이거나 지워지지 않는다.
        await self._stop_loop(it)
        self.persist()
        if it.engine.shares > 0 and it.code not in self.hub.stocks:
            self.hub.add_stock(it.code, it.name)
            self._log(it.code, "[매매] 목록으로 이동(수동매매)")
        self.broadcast(it.code)

    # -- 틱·체결 -------------------------------------------------------------
    def _start_loop(self, it: CandleItem) -> None:
        if it.task is None or it.task.done():
            it.task = asyncio.create_task(self._tick_loop(it))

    async def _stop_loop(self, it: CandleItem) -> None:
        task, it.task = it.task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _tick_loop(self, it: CandleItem) -> None:
        try:
            # aclosing: 종료 시 구독 해제(finally)가 즉시 돌게 한다. 그냥 break 하면
            # 제너레이터 정리가 GC 시점으로 밀려, 뒤이어 [매매]가 연 같은 종목
            # 구독을 늦게 지울 수 있다.
            async with aclosing(self.hub.data.stream_ticks(it.code)) as ticks:
                async for tick in ticks:
                    self.hub.broadcast({"type": "tick", "tick": tick.model_dump()})
                    if self.hub.clock.is_open():
                        await self._on_tick(it, tick)
                    if not it.engine.phase.active:
                        break
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("[%s] 대금 양봉 매매 틱 루프 오류", it.code)
            self._log(it.code, "⚠️ 틱 처리 오류 — 로그 확인 필요")

    async def _on_tick(self, it: CandleItem, tick: Tick) -> None:
        async with it.lock:
            eng = it.engine
            if not eng.phase.active:
                return
            if not it.verified:
                if not await self._retry_verify(it):
                    return
                if not eng.phase.active:
                    return
            today = self._today()
            before = (eng.phase, eng.shares)
            if eng.needs_day(today):
                if not await self._begin_day(it, today):
                    return
                if eng.phase != before[0]:          # 기한 만료 등
                    self.persist()
                    self.broadcast(it.code)
                    return
                self.persist()
                self.broadcast(it.code)
            if (eng.phase == CzPhase.WATCHING and eng.in_zone(tick.price)
                    and self._should_yield_to_ulc(it)):
                return
            await eng.on_tick(tick, today, self._buy_fn(it), self._sell_fn(it))
            if eng.phase == CzPhase.DONE and before[0] != CzPhase.DONE and eng.bought_qty > 0:
                await self._after_exit(it)
            elif eng.phase == CzPhase.MANUAL and before[0] != CzPhase.MANUAL:
                it.notice = eng.exit_reason
                asyncio.get_running_loop().create_task(self._move_to_manual(it))
            if (eng.phase, eng.shares) != before:
                self.persist()
                await self._broadcast_with_position(it.code)

    async def _begin_day(self, it: CandleItem, today: str) -> bool:
        """일봉으로 오늘의 맥락(전일 종가, 기준일 이후 경과 거래일)을 엔진에 넣는다.
        조회 실패면 False — 그동안은 평가하지 않고 ``DAY_RETRY_SEC`` 뒤 다시 시도한다
        (기한·손절 판단 근거 없이 주문하지 않는다)."""
        now = time.monotonic()
        if it.day_checked_at and now - it.day_checked_at < self.DAY_RETRY_SEC:
            return False
        it.day_checked_at = now
        eng = it.engine
        try:
            bars = await asyncio.to_thread(self.hub.data.get_bars, it.code, DAY_INTERVAL,
                                           self.DAY_LOOKBACK)
        except Exception as e:  # noqa: BLE001
            self._log(it.code, f"⚠️ 일봉 조회 실패 — 잠시 후 재시도({e})")
            return False
        days = sorted({_bar_date(b.time): b for b in bars}.items())
        if today > eng.base_date and not eng.base_confirmed and not eng.phase.holding:
            base = dict(days).get(eng.base_date)
            if base is not None:
                if base.close > base.open:
                    if (base.open, base.close) != (eng.base_open, eng.base_close):
                        self._log(it.code, f"기준 캔들 확정값 반영: 시가 {base.open:,.0f} 종가 {base.close:,.0f}"
                                           f" (등록 시 {eng.base_open:,.0f}/{eng.base_close:,.0f})")
                    eng.set_base(eng.base_date, base.open, base.close, confirmed=True)
                else:
                    eng.phase = CzPhase.DONE
                    eng.exit_reason = f"기준일({eng.base_date}) 일봉이 양봉으로 끝나지 않음"
                    self._log(it.code, eng.exit_reason)
                    return True
        prev = [b for d, b in days if d < today]
        prev_close = prev[-1].close if prev else None
        elapsed = 0 if today <= eng.base_date else \
            sum(1 for d, _ in days if eng.base_date < d < today) + 1
        eng.begin_day(today, prev_close, elapsed)
        it.day_checked_at = 0.0
        return True

    async def _retry_verify(self, it: CandleItem) -> bool:
        now = time.monotonic()
        if now - it.verify_checked_at < self.VERIFY_RETRY_SEC:
            return False
        it.verify_checked_at = now
        try:
            pos = await asyncio.to_thread(self.hub.broker.position, it.code)
        except Exception:  # noqa: BLE001
            return False
        self._reconcile(it, pos.quantity)
        self.persist()
        self.broadcast(it.code)
        return it.verified and it.engine.phase.active

    async def _after_exit(self, it: CandleItem) -> None:
        """청산 후 계좌로 확인. 잔량이 남았으면 수동 인계(재매도는 걸지 않는다 —
        매도 주문이 아직 미체결일 수 있어 중복 매도 위험)."""
        self.hub.broker.invalidate()
        try:
            pos = await asyncio.to_thread(self.hub.broker.position, it.code)
        except Exception:  # noqa: BLE001
            pos = None
        if pos is None:
            it.notice = "청산 후 계좌 잔고 확인 실패 — 계좌에서 직접 확인하세요."
            self._log(it.code, f"⚠️ {it.notice}")
        elif pos.quantity > 0:
            it.notice = f"청산 후 계좌 잔량 {pos.quantity}주(부분체결/미체결 가능) — [매매]에서 확인"
            self._log(it.code, f"⚠️ {it.notice}")
            it.engine.phase = CzPhase.MANUAL
            asyncio.get_running_loop().create_task(self._move_to_manual(it))
        else:
            self._log(it.code, f"청산 완료({it.engine.exit_reason})")

    def on_fill(self, fill: dict) -> None:
        code = fill.get("code")
        it = self.items.get(code)
        if it is None:
            return
        side = "매수" if fill.get("side") == "buy" else "매도"
        self._log(code, f"체결: {side} {fill.get('qty')}주 @ {fill.get('price', 0):,.0f} "
                        f"(미체결 {fill.get('unfilled')})")
        try:
            it.engine.on_fill(str(fill.get("side") or ""), int(fill.get("qty") or 0),
                              float(fill.get("price") or 0.0))
        except Exception:  # noqa: BLE001
            logger.exception("[%s] 대금 양봉 매매 체결 반영 실패", code)
        self.persist()
        asyncio.get_running_loop().create_task(self._broadcast_with_position(code))

    # -- 주문 콜백 -----------------------------------------------------------
    def _buy_fn(self, it: CandleItem):
        async def buy_fn(amount: int) -> float:
            res = await asyncio.to_thread(self.hub.broker.buy, it.code, amount)
            if not res.ok:
                self._log(it.code, f"매수 실패: {res.message}")
            return res.price if res.ok else 0.0
        return buy_fn

    def _sell_fn(self, it: CandleItem):
        async def sell_fn(qty: int) -> int:
            """주문한 수량을 돌려준다(실패 0)."""
            # 엔진 수량과 계좌 잔고 중 작은 쪽만 판다(남의 물량·과다 매도 방지).
            try:
                pos = await asyncio.to_thread(self.hub.broker.position, it.code)
                qty = min(qty, pos.quantity)
            except Exception:  # noqa: BLE001
                pass
            if qty <= 0:
                self._log(it.code, "⚠️ 매도할 계좌 잔고가 없습니다")
                return 0
            res = await asyncio.to_thread(self.hub.broker.sell, it.code, qty)
            if not res.ok:
                self._log(it.code, f"매도 실패: {res.message}")
            return qty if res.ok else 0
        return sell_fn

    # -- 기타 ----------------------------------------------------------------
    async def _broadcast_with_position(self, code: str) -> None:
        self.broadcast(code)
        # 포지션 표시는 [매매]와 같은 status 경로가 아니므로 카드 전용 필드로 보낸다.
        try:
            pos = await asyncio.to_thread(self.hub.broker.position, code)
        except Exception:  # noqa: BLE001
            return
        self.hub.broadcast({"type": "candle_trade_position", "code": code,
                            "position": pos.model_dump()})

    def _engine_log(self, code: str):
        return lambda text: self._log(code, text)

    def _get(self, code: str) -> CandleItem:
        it = self.items.get(code)
        if it is None:
            raise KeyError(code)
        return it

    @staticmethod
    def _today() -> str:
        return now_kst().date().isoformat()

    async def close(self) -> None:
        for it in list(self.items.values()):
            await self._stop_loop(it)
