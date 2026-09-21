"""정규장 종료 후 상한가 목록을 하루 한 번 저장한다."""
import asyncio
import logging
from datetime import datetime, time

from .market_clock import now_kst
from .screener import (
    MIN_PCT_DEFAULT, MAX_PCT_DEFAULT, screen_current_upper_limits, source_name,
)
from .screener_cache import load_snapshot, save_snapshot

logger = logging.getLogger(__name__)
# 마감 직후 API 반영 시간을 두고 수집한다. 실패하면 다음 주기에 재시도한다.
COLLECT_AFTER = time(15, 35)


async def collect_once(manager):
    at = now_kst()
    if source_name() != "krx" or at.weekday() >= 5 or at.time() < COLLECT_AFTER:
        return
    hub = next((manager.hubs[c.id] for c in manager.configs
                if c.provider == "kiwoom" and not c.kiwoom_mock), None)
    if hub is None:
        return
    saved = await asyncio.to_thread(load_snapshot, at.date(), MIN_PCT_DEFAULT, MAX_PCT_DEFAULT)
    if saved is not None:
        captured = datetime.fromisoformat(saved.captured_at)
        if captured.date() == at.date() and captured.time() >= COLLECT_AFTER:
            return
    current = await asyncio.to_thread(hub.data.current_upper_limits)
    if current is None:
        raise RuntimeError("키움 상한가 조회 실패")
    result = await asyncio.to_thread(
        screen_current_upper_limits, current, MIN_PCT_DEFAULT, MAX_PCT_DEFAULT,
        today=at.date(),
    )
    # 날짜를 지정할 수 없는 API이므로 자정을 걸친 응답은 보관하지 않는다.
    if now_kst().date() != at.date():
        return
    if not await asyncio.to_thread(save_snapshot, result, at):
        raise RuntimeError("상한가 결과 파일 저장 실패")
    logger.info("상한가 자동 수집 완료: %s, %d종목", result.date, len(result.stocks))


async def run_collector(manager):
    while True:
        try:
            await collect_once(manager)
        except Exception:
            logger.warning("상한가 자동 수집 실패: 60초 후 재시도", exc_info=True)
        await asyncio.sleep(60)
