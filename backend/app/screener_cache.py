"""날짜와 조회 조건이 일치하는 키움 결과를 재시작 후에도 보존한다."""
import logging
import os
from datetime import datetime
from pathlib import Path
from threading import Lock

from .models import UpperLimitResult

_lock = Lock()
logger = logging.getLogger(__name__)


REGULAR = "regular"   # 정규장 종가 기준 상한가 목록(15:30~15:40 수집)


def _path(day, minimum, maximum, kind=""):
    root = Path(os.getenv("SCREENER_CACHE_DIR", str(Path(__file__).resolve().parents[1])))
    prefix = f"state.screener.{day}." + (f"{kind}." if kind else "")
    return root / f"{prefix}{float(minimum)}.{float(maximum)}.json"


def save_snapshot(result: UpperLimitResult, at: datetime, kind: str = "") -> bool:
    """``kind``='' 는 마지막 거래가(통합) 기준 결과, REGULAR 는 정규장 종가 기준 목록."""
    result.captured_at = at.isoformat()
    path = _path(result.date, result.min_pct, result.max_pct, kind)
    try:
        with _lock:
            # 느린 이전 요청이 나중 요청의 결과를 덮어쓰지 않는다.
            if path.exists():
                try:
                    old = UpperLimitResult.model_validate_json(path.read_text(encoding="utf-8"))
                except ValueError:
                    old = None
                if old is not None and old.captured_at > result.captured_at:
                    return True
            temporary = path.with_suffix(".pending.json")
            temporary.write_text(result.model_dump_json(), encoding="utf-8")
            temporary.replace(path)
        return True
    except (OSError, ValueError):
        logger.warning("상한가 조회 결과 저장 실패: %s", path, exc_info=True)
        return False


def load_snapshot(day, minimum, maximum, kind: str = ""):
    path = _path(day, minimum, maximum, kind)
    try:
        with _lock:
            result = UpperLimitResult.model_validate_json(path.read_text(encoding="utf-8"))
        if (result.date != str(day) or result.source != "kiwoom"
                or result.min_pct != minimum or result.max_pct != maximum
                or not result.captured_at):
            return None
        result.cached = True
        result.notice = (
            f"KRX 종가 자료가 아직 없어 {result.captured_at[:19].replace('T', ' ')} "
            "(한국시간)에 조회한 키움 시세를 표시합니다. 확정 종가가 아니며, "
            "장중에 저장된 결과는 마감 결과와 다를 수 있습니다."
        )
        return result
    except (OSError, ValueError):
        return None
