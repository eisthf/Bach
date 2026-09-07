"""상한가 종목 스크리너.

지정일 **D**의 종가가 직전 거래일 **D-1**의 종가 대비 ``[min_pct, max_pct]``%
오른 종목을 찾는다. 기본 29~30%는 KRX 가격제한폭(+30%)에 호가단위 반올림
오차를 감안한 '상한가' 판정 구간이다(정확히 30%가 되는 호가는 드물다).

D-1은 **달력상 전날이 아니라 직전 거래일**이다. 주말·공휴일·대체휴일은
데이터가 비어 오는 것으로 판별해 건너뛴다(휴장일 달력을 따로 들고 있지 않다).

데이터 소스:
- ``krx``  : KRX Open API 전종목 일별매매정보. ``KRX_OPEN_API_KEY`` 필요.
- ``kiwoom``: 실전 계좌의 ka10017 당일 상한가(오늘 조회에만 자동 사용).
- ``mock`` : 합성 데이터. 키 없이 UI/흐름을 확인하기 위한 데모용.
환경변수 ``SCREENER_SOURCE``는 ``krx|mock`` 중 하나로 데이터 환경을 고른다.
미지정이면 키가 있을 때 krx, 없으면 mock으로 자동 선택한다. 응답의 ``source``
필드로 어느 쪽인지 항상 알 수 있고, 프런트는 mock이면 '데모 데이터' 배지를
띄운다 — 합성값을 실데이터로 오인하는 게 가장 위험하기 때문이다.

⚠️ 단순 종가 비교라, 액면분할·병합·증자로 기준가가 조정된 종목은 등락률이
   왜곡될 수 있다(그런 날은 전일 종가와 기준가가 다르다).
"""
from __future__ import annotations

import logging
import os
import random
import zlib
from datetime import date as _date, datetime, timedelta
from typing import Callable, List, Optional

from .market_clock import REGULAR_OPEN, now_kst
from .models import UpperLimitResult, UpperLimitStock
from .providers.krx_api import DailyQuote, configured, daily_quotes

logger = logging.getLogger("bach.screener")

MIN_PCT_DEFAULT = 29.0
MAX_PCT_DEFAULT = 30.0

# 거래일 탐색 상한(일). 설/추석 연휴 + 주말이 겹쳐도 넉넉하다.
LOOKBACK_LIMIT = 14

# 날짜 문자열 → DailyQuote 목록. 테스트에서 갈아끼운다.
Fetch = Callable[[str], List[DailyQuote]]


class ScreenerError(Exception):
    """스크리너 입력/조회 실패."""


class NotATradingDay(ScreenerError):
    """지정일에 시세가 없다(휴장일이거나 아직 게시 전)."""


class CurrentDataUnavailable(ScreenerError):
    """실전 계좌의 키움 당일 상한가 조회가 실패했다."""


# ---------------------------------------------------------------------------
# 날짜 유틸
# ---------------------------------------------------------------------------
def parse_date(value: str) -> _date:
    """'YYYY-MM-DD' 또는 'YYYYMMDD' → date."""
    s = (value or "").strip().replace("-", "").replace("/", "")
    if len(s) != 8 or not s.isdigit():
        raise ScreenerError(f"날짜 형식이 올바르지 않습니다: {value!r} (YYYY-MM-DD)")
    try:
        return datetime.strptime(s, "%Y%m%d").date()
    except ValueError as e:
        raise ScreenerError(f"존재하지 않는 날짜입니다: {value!r}") from e


def _compact(d: _date) -> str:
    return d.strftime("%Y%m%d")


def _iso(d: _date) -> str:
    return d.strftime("%Y-%m-%d")


def latest_session_date(at: datetime) -> _date:
    """현재 시각 기준 가장 최근 정규장 세션 날짜(주말 포함 장전 롤오버 보정)."""
    d = at.date()
    if d.weekday() >= 5 or at.time() < REGULAR_OPEN:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _walk_back(fetch: Fetch, start: _date) -> tuple[_date, List[DailyQuote]]:
    """``start``부터 하루씩 거슬러 시세가 있는 첫 날을 찾는다."""
    d = start
    for _ in range(LOOKBACK_LIMIT + 1):
        quotes = fetch(_compact(d))
        if quotes:
            return d, quotes
        d -= timedelta(days=1)
    raise NotATradingDay(
        f"{_iso(start)}부터 {LOOKBACK_LIMIT}일을 거슬러도 시세가 없습니다. "
        "데이터 소스 상태를 확인하세요."
    )


# ---------------------------------------------------------------------------
# 합성(mock) 소스 — 키 없이 UI/흐름을 보기 위한 데모 데이터
# ---------------------------------------------------------------------------
_MOCK_UNIVERSE = 300      # 합성 전종목 수
_MOCK_HITS = 7            # 하루에 상한가로 만들 종목 수

_MOCK_HEAD = ["가온", "나린", "다솜", "라온", "미르", "바림", "사담", "아람",
              "차온", "카린", "타래", "파람", "하람", "누리", "고운", "여울"]
_MOCK_TAIL = ["전자", "바이오", "화학", "소재", "테크", "에너지", "제약",
              "로보틱스", "반도체", "네트웍스", "머티리얼즈", "홀딩스"]


def _rng(*parts) -> random.Random:
    """부분 키들로 재현 가능한 난수기를 만든다(mock provider와 같은 방식)."""
    seed = zlib.crc32("|".join(str(p) for p in parts).encode())
    return random.Random(seed)


def _mock_code(i: int) -> str:
    return f"9{i:05d}"


def _mock_name(i: int) -> str:
    r = _rng("name", i)
    return r.choice(_MOCK_HEAD) + r.choice(_MOCK_TAIL)


def _mock_prev_weekday(d: _date) -> _date:
    p = d - timedelta(days=1)
    while p.weekday() >= 5:
        p -= timedelta(days=1)
    return p


def _mock_hit_set(d: _date) -> set[int]:
    """그 날 상한가로 만들 종목 인덱스."""
    return set(_rng("hit", _compact(d)).sample(range(_MOCK_UNIVERSE), _MOCK_HITS))


def _mock_plain_close(i: int, d: _date) -> int:
    """상한가 보정 전의 그 날 종가."""
    base = _rng("base", i).randint(1_000, 90_000)
    drift = _rng("move", i, _compact(d)).uniform(-0.06, 0.06)
    return max(100, round(base * (1 + drift)))


def _mock_quotes(date: str) -> List[DailyQuote]:
    """합성 전종목 시세. 주말은 빈 리스트(= 휴장)."""
    d = parse_date(date)
    if d.weekday() >= 5:
        return []

    prev = _mock_prev_weekday(d)
    # 이틀 연속 상한가면 D-1 종가가 이미 보정값이라 D의 상승률이 어긋난다.
    # 전날 상한가였던 종목은 오늘 대상에서 뺀다(재귀 없이 정확도 확보).
    hits = _mock_hit_set(d) - _mock_hit_set(prev)

    out: List[DailyQuote] = []
    for i in range(_MOCK_UNIVERSE):
        if i in hits:
            pct = _rng("pct", i, _compact(d)).uniform(0.290, 0.2998)
            close = max(100, round(_mock_plain_close(i, prev) * (1 + pct)))
        else:
            close = _mock_plain_close(i, d)
        shares = _rng("shares", i).randint(3_000_000, 900_000_000)
        r = _rng("ohlc", i, _compact(d))
        low = round(close * r.uniform(0.93, 0.995))
        high = round(close * r.uniform(1.005, 1.07))
        out.append(DailyQuote(
            code=_mock_code(i),
            name=_mock_name(i),
            market="KOSPI" if i % 3 == 0 else "KOSDAQ",
            open=round(close * r.uniform(0.95, 1.05)),
            high=high,
            low=low,
            close=close,
            volume=r.randint(10_000, 40_000_000),
            market_cap=shares * close,
            listed_shares=shares,
        ))
    return out


# ---------------------------------------------------------------------------
# 소스 선택
# ---------------------------------------------------------------------------
def source_name() -> str:
    """설정된 소스 이름('krx' | 'mock')."""
    src = (os.getenv("SCREENER_SOURCE") or "").strip().lower()
    if src in ("krx", "mock"):
        return src
    # 미지정: 키가 있으면 실데이터, 없으면 데모.
    return "krx" if configured() else "mock"


def _fetch_for(source: str) -> Fetch:
    return daily_quotes if source == "krx" else _mock_quotes


# ---------------------------------------------------------------------------
# 스크리닝
# ---------------------------------------------------------------------------
def screen_upper_limit(
    date: Optional[str] = None,
    min_pct: float = MIN_PCT_DEFAULT,
    max_pct: float = MAX_PCT_DEFAULT,
    fetch: Optional[Fetch] = None,
    source: Optional[str] = None,
) -> UpperLimitResult:
    """D일 종가가 직전 거래일 대비 ``min_pct``~``max_pct``% 오른 종목을 찾는다.

    ``date``를 생략하면 오늘(KST)부터 거슬러 시세가 있는 **가장 최근 거래일**을
    D로 잡는다 — 장 마감 후 게시 전이면 자연히 전 거래일로 내려간다.
    """
    if min_pct > max_pct:
        raise ScreenerError(f"min_pct({min_pct})가 max_pct({max_pct})보다 큽니다.")

    src = source or source_name()
    get = fetch or _fetch_for(src)

    if date:
        d = parse_date(date)
        quotes_d = get(_compact(d))
        if not quotes_d:
            raise NotATradingDay(
                f"{_iso(d)}은(는) 장이 서지 않았거나 시세가 아직 게시되지 않았습니다."
            )
    else:
        d, quotes_d = _walk_back(get, now_kst().date())

    prev_d, quotes_prev = _walk_back(get, d - timedelta(days=1))

    prev_close = {q.code: q.close for q in quotes_prev}
    rows: List[UpperLimitStock] = []
    for q in quotes_d:
        base = prev_close.get(q.code, 0)
        if base <= 0 or q.close <= 0:
            continue  # 신규상장·거래정지 등 전일 종가가 없는 종목
        # 표시값(소수 2자리)으로 판정한다. 부동소수 오차 때문에 정확히 29%인
        # 종목이 28.999999...로 계산돼 탈락하면, 화면엔 29.00%인데 목록에는
        # 없는 모순이 생긴다.
        pct = round((q.close - base) / base * 100, 2)
        if min_pct <= pct <= max_pct:
            rows.append(UpperLimitStock(
                code=q.code,
                name=q.name,
                market=q.market,
                close=q.close,
                prev_close=base,
                change_pct=pct,
                volume=q.volume,
                market_cap=q.market_cap,
            ))

    rows.sort(key=lambda r: (-r.change_pct, -r.market_cap))
    logger.info("상한가 스크리닝 %s (기준 %s): %d/%d 종목 [%s]",
                _iso(d), _iso(prev_d), len(rows), len(quotes_d), src)
    return UpperLimitResult(
        date=_iso(d),
        prev_date=_iso(prev_d),
        min_pct=min_pct,
        max_pct=max_pct,
        source=src,
        scanned=len(quotes_d),
        stocks=rows,
    )


def screen_current_upper_limits(
    current: List[dict],
    min_pct: float = MIN_PCT_DEFAULT,
    max_pct: float = MAX_PCT_DEFAULT,
    fetch: Optional[Fetch] = None,
    today: Optional[_date] = None,
) -> UpperLimitResult:
    """키움 당일 상한가 스냅샷을 KRX 직전 거래일 종목정보로 보완한다."""
    if min_pct > max_pct:
        raise ScreenerError(f"min_pct({min_pct})가 max_pct({max_pct})보다 큽니다.")
    d = today or now_kst().date()
    get = fetch or daily_quotes
    prev_d, previous = _walk_back(get, d - timedelta(days=1))
    metadata = {q.code: q for q in previous}

    rows: List[UpperLimitStock] = []
    for item in current:
        code = str(item.get("code") or "").strip()
        price = int(item.get("price") or 0)
        pct = round(float(item.get("change_pct") or 0), 2)
        if not code or price <= 0 or not min_pct <= pct <= max_pct:
            continue
        old = metadata.get(code)
        prev_close = int(item.get("prev_close") or 0)
        if prev_close <= 0 and old is not None:
            prev_close = old.close
        shares = old.listed_shares if old is not None else 0
        rows.append(UpperLimitStock(
            code=code,
            name=str(item.get("name") or (old.name if old else "")),
            market=old.market if old else "",
            close=price,
            prev_close=prev_close,
            change_pct=pct,
            volume=int(item.get("volume") or 0),
            market_cap=shares * price,
        ))

    rows.sort(key=lambda r: (-r.change_pct, -r.market_cap))
    logger.info("당일 상한가 조회 %s (기준 %s): %d/%d 종목 [kiwoom]",
                _iso(d), _iso(prev_d), len(rows), len(current))
    return UpperLimitResult(
        date=_iso(d), prev_date=_iso(prev_d), min_pct=min_pct, max_pct=max_pct,
        source="kiwoom", snapshot=True, scanned=len(current), stocks=rows,
    )
