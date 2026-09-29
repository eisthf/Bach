"""정규장 종료 후 상한가 목록을 저장한다.

하루에 세 가지를 남긴다.
- 15:30:30~15:40 **정규장 상한가 목록**(KRX, ``REGULAR``): KRX 시간외 종가 매매(15:40~)
  전이라 정규장 종가 기준이다. 종목별 15:30 봉으로 다시 확인한다. 나중에 시간외에서
  밀린 종목(정규장 상한가·시간외 이탈)을 가려내는 기준이 된다.
- 15:35 이후 **마지막 거래가(통합) 기준 결과**: 서버가 꺼지기 전 확보용.
- 20:05 이후 **최종 결과**: NXT 애프터마켓(~20:00)까지 끝난 마지막 거래가 기준.
실패하면 다음 주기(60초)에 재시도한다.
"""
import asyncio
import logging
from datetime import datetime, time
from typing import Optional

from .market_clock import REGULAR_CLOSE, now_kst
from .models import UpperLimitResult
from .screener import (
    MIN_PCT_DEFAULT, MAX_PCT_DEFAULT, add_after_hours_drops, screen_current_upper_limits,
    source_name,
)
from .screener_cache import REGULAR, load_snapshot, save_snapshot

logger = logging.getLogger(__name__)
REGULAR_AFTER = time(15, 30, 30)   # KRX 종가 단일가 체결 직후
REGULAR_BEFORE = time(15, 40)      # KRX 장후 시간외 종가 매매 시작 전
# 마감 직후 API 반영 시간을 두고 수집한다.
COLLECT_AFTER = time(15, 35)
FINAL_AFTER = time(20, 5)          # NXT 애프터마켓(20:00) 종료 후


def live_upper_limits(data, min_pct: float, max_pct: float, at: datetime) -> Optional[UpperLimitResult]:
    """(블로킹) 키움 통합 당일 상한가 + 정규장 상한가·시간외 이탈 종목. 조회 실패는 None."""
    current = data.current_upper_limits()
    if current is None:
        return None
    result = screen_current_upper_limits(current, min_pct, max_pct, today=at.date())
    if at.time() >= REGULAR_CLOSE:
        regular = load_snapshot(at.date(), min_pct, max_pct, kind=REGULAR)
        add_after_hours_drops(result, regular, getattr(data, "last_price", None))
    return result


def _captured(saved, at: datetime):
    if saved is None:
        return None
    captured = datetime.fromisoformat(saved.captured_at)
    return captured.time() if captured.date() == at.date() else None


async def _collect_regular(hub, at: datetime) -> None:
    if load_snapshot(at.date(), MIN_PCT_DEFAULT, MAX_PCT_DEFAULT, kind=REGULAR) is not None:
        return
    current = await asyncio.to_thread(hub.data.current_upper_limits, True)
    if current is None:
        raise RuntimeError("키움 정규장 상한가 조회 실패")
    result = await asyncio.to_thread(
        screen_current_upper_limits, current, MIN_PCT_DEFAULT, MAX_PCT_DEFAULT, today=at.date(),
    )
    # 15:30 봉으로 확인한 정규장 종가가 상한가 구간 밖이면 정규장 상한가가 아니다.
    result.stocks = [s for s in result.stocks if not s.after_hours]
    if now_kst().date() != at.date():
        return
    if not await asyncio.to_thread(save_snapshot, result, at, REGULAR):
        raise RuntimeError("정규장 상한가 목록 저장 실패")
    logger.info("정규장 상한가 목록 저장: %s, %d종목", result.date, len(result.stocks))


async def _collect_latest(hub, at: datetime) -> None:
    saved = await asyncio.to_thread(load_snapshot, at.date(), MIN_PCT_DEFAULT, MAX_PCT_DEFAULT)
    captured = _captured(saved, at)
    target = FINAL_AFTER if at.time() >= FINAL_AFTER else COLLECT_AFTER
    if captured is not None and captured >= target:
        return
    result = await asyncio.to_thread(live_upper_limits, hub.data, MIN_PCT_DEFAULT,
                                     MAX_PCT_DEFAULT, at)
    if result is None:
        raise RuntimeError("키움 상한가 조회 실패")
    # 날짜를 지정할 수 없는 API이므로 자정을 걸친 응답은 보관하지 않는다.
    if now_kst().date() != at.date():
        return
    if not await asyncio.to_thread(save_snapshot, result, at):
        raise RuntimeError("상한가 결과 파일 저장 실패")
    logger.info("상한가 자동 수집 완료: %s, %d종목", result.date, len(result.stocks))


async def collect_once(manager):
    at = now_kst()
    if source_name() != "krx" or at.weekday() >= 5 or at.time() < REGULAR_AFTER:
        return
    hub = next((manager.hubs[c.id] for c in manager.configs
                if c.provider == "kiwoom" and not c.kiwoom_mock), None)
    if hub is None:
        return
    if at.time() < REGULAR_BEFORE:
        await _collect_regular(hub, at)
    if at.time() >= COLLECT_AFTER:
        await _collect_latest(hub, at)


async def run_collector(manager):
    while True:
        try:
            await collect_once(manager)
        except Exception:
            logger.warning("상한가 자동 수집 실패: 60초 후 재시도", exc_info=True)
        await asyncio.sleep(60)
