"""KRX Open API(전종목 일별매매정보) 클라이언트 — self-contained.

상한가 스크리너(`app/screener.py`)의 실데이터 소스. **하루치 전종목 시세를
시장당 1회 호출**로 받아온다.

왜 키움이 아니라 KRX인가: 키움 REST에는 전종목 일별시세 엔드포인트가 없다.
종목별 조회(ka10081/ka10086)뿐이라 날짜 하나당 ~2,800회를 호출해야 하고,
전종목 순위 API(ka10017 상하한가, ka10027 등락률상위)는 *당일*만 조회되어
과거 날짜 지정이 불가능하다.

⚠️ 준비물 — 환경변수 ``KRX_OPEN_API_KEY``:
  1. https://openapi.krx.co.kr 에서 인증키 발급
  2. **'유가증권 일별매매정보'와 '코스닥 일별매매정보' 각각** 이용신청 → 승인 대기
  키가 없거나 서비스 승인 전이면 401 ``Unauthorized Key`` 가 떨어진다.

⚠️ 함정:
1. **숫자가 전부 쉼표 포함 문자열**이다("1,234,567") → ``_num()`` 으로 파싱.
2. **종목코드 필드명이 응답마다 다르다**(``ISU_SRT_CD``/``ISU_CD``). ISIN(12자리,
   ``KR7005930003``)으로 올 수 있어 ``[3:9]`` 로 단축코드를 뽑는다.
3. **휴장일은 에러가 아니다** — HTTP 200 + 빈 ``OutBlock_1``. 빈 리스트로 구분한다.
4. **시가총액(MKTCAP)이 빠진 응답**이 있어 상장주식수×종가로 폴백한다.
"""
from __future__ import annotations

import logging
import os
import threading
import time as _time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("bach.krx")

_BASE = "https://data-dbg.krx.co.kr/svc/apis/sto"
# 시장별 '일별매매정보' 엔드포인트. 각각 따로 이용신청/승인이 필요하다.
_ENDPOINTS = {
    "KOSPI": "/stk_bydd_trd",
    "KOSDAQ": "/ksq_bydd_trd",
}
_TIMEOUT = 30.0

# 확정된 과거 영업일 데이터는 불변이라 영구 캐시한다. 오늘 날짜와 빈 응답
# (아직 게시 전이거나 휴장일)만 TTL을 두고 다시 물어본다.
_FRESH_TTL = 300.0


class KrxError(RuntimeError):
    """KRX Open API 호출 실패."""


class KrxAuthError(KrxError):
    """인증키 누락/거부. 사용자가 키를 발급·승인받아야 풀린다."""


@dataclass(frozen=True)
class DailyQuote:
    """한 종목의 하루치 시세."""
    code: str
    name: str
    market: str          # "KOSPI" | "KOSDAQ"
    open: int
    high: int
    low: int
    close: int
    volume: int
    market_cap: int      # 시가총액(원)
    listed_shares: int   # 상장주식수


def api_key() -> str:
    return (os.getenv("KRX_OPEN_API_KEY") or "").strip()


def configured() -> bool:
    """인증키가 설정돼 있는가(유효한지는 호출해 봐야 안다)."""
    return bool(api_key())


# ---------------------------------------------------------------------------
# 파싱
# ---------------------------------------------------------------------------
def _num(val) -> int:
    """쉼표 포함 숫자 문자열 → int. 파싱 불가는 0."""
    s = str(val or "").strip().replace(",", "")
    if not s or s in ("-", "nan", "None"):
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _short_code(raw) -> str:
    """ISIN(12자리)이면 단축코드(6자리)를 뽑고, 아니면 6자리로 zero-pad."""
    s = str(raw or "").strip()
    if len(s) == 12:
        return s[3:9]
    return s.zfill(6) if s else ""


def _first(row: dict, *keys: str) -> str:
    """응답마다 다른 필드명 중 먼저 채워진 값을 고른다."""
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _to_quote(row: dict, market: str) -> Optional[DailyQuote]:
    code = _short_code(_first(row, "ISU_SRT_CD", "ISU_CD"))
    if not code:
        return None
    close = _num(row.get("TDD_CLSPRC"))
    shares = _num(row.get("LIST_SHRS"))
    # MKTCAP 이 비어 오는 응답이 있어 상장주식수×종가로 메운다.
    cap = _num(row.get("MKTCAP")) or shares * close
    return DailyQuote(
        code=code,
        name=_first(row, "ISU_ABBRV", "ISU_NM"),
        market=market,
        open=_num(row.get("TDD_OPNPRC")),
        high=_num(row.get("TDD_HGPRC")),
        low=_num(row.get("TDD_LWPRC")),
        close=close,
        volume=_num(row.get("ACC_TRDVOL")),
        market_cap=cap,
        listed_shares=shares,
    )


def parse_rows(rows: list, market: str) -> List[DailyQuote]:
    """OutBlock_1 행 목록 → DailyQuote 목록 (테스트에서도 쓴다)."""
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        q = _to_quote(row, market)
        if q is not None:
            out.append(q)
    return out


# ---------------------------------------------------------------------------
# 호출
# ---------------------------------------------------------------------------
def _fetch_market(date: str, market: str) -> List[DailyQuote]:
    """한 시장의 하루치 전종목 시세. 휴장일이면 빈 리스트."""
    key = api_key()
    if not key:
        raise KrxAuthError(
            "KRX_OPEN_API_KEY 가 설정되지 않았습니다. "
            "openapi.krx.co.kr 에서 인증키를 발급받고 '유가증권/코스닥 일별매매정보' "
            "서비스 이용신청(승인 필요)을 마친 뒤 backend/.env 에 넣으세요."
        )
    url = _BASE + _ENDPOINTS[market]
    try:
        resp = requests.post(
            url,
            headers={"AUTH_KEY": key, "Content-Type": "application/json"},
            json={"basDd": date},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as e:
        raise KrxError(f"KRX Open API 연결 실패({market}): {e}") from e

    if resp.status_code in (401, 403):
        raise KrxAuthError(
            f"KRX Open API 인증 거부({resp.status_code} {resp.text[:120]}). "
            "인증키가 만료됐거나 '유가증권/코스닥 일별매매정보' 이용신청이 "
            "승인되지 않은 상태입니다."
        )
    if resp.status_code != 200:
        raise KrxError(f"KRX Open API 오류({market}): HTTP {resp.status_code} {resp.text[:200]}")

    try:
        payload = resp.json()
    except ValueError as e:
        raise KrxError(f"KRX Open API 응답 파싱 실패({market}): {resp.text[:200]}") from e

    # 본문에 에러코드가 실려 오는 경우(HTTP 200 + respCode).
    resp_code = str(payload.get("respCode") or "").strip()
    if resp_code and resp_code not in ("0", "00", "000"):
        msg = str(payload.get("respMsg") or "")
        if resp_code in ("401", "403") or "nauthorized" in msg:
            raise KrxAuthError(f"KRX Open API 인증 거부({market}): {resp_code} {msg}")
        raise KrxError(f"KRX Open API 오류({market}): {resp_code} {msg}")

    rows = payload.get("OutBlock_1") or []
    return parse_rows(rows, market)


# 날짜별 캐시 + 날짜별 락(동시 요청 병합). kiwoom.py 의 봉 캐시와 같은 방식.
_cache: Dict[str, Tuple[float, List[DailyQuote]]] = {}
_date_locks: Dict[str, threading.Lock] = {}
_guard = threading.Lock()


def _lock_for(date: str) -> threading.Lock:
    with _guard:
        lk = _date_locks.get(date)
        if lk is None:
            lk = _date_locks[date] = threading.Lock()
        return lk


def clear_cache() -> None:
    with _guard:
        _cache.clear()
        _date_locks.clear()


def daily_quotes(date: str) -> List[DailyQuote]:
    """``date``(YYYYMMDD)의 전종목(코스피+코스닥) 시세. 휴장일이면 빈 리스트.

    확정 데이터는 영구 캐시하고, 빈 응답(미게시/휴장)만 TTL 후 재조회한다.
    """
    now = _time.monotonic()
    with _guard:
        hit = _cache.get(date)
    if hit and (hit[1] or now - hit[0] < _FRESH_TTL):
        return hit[1]

    with _lock_for(date):
        # 락을 기다리는 동안 다른 스레드가 채웠을 수 있다.
        with _guard:
            hit = _cache.get(date)
        if hit and (hit[1] or _time.monotonic() - hit[0] < _FRESH_TTL):
            return hit[1]

        by_market = {m: _fetch_market(date, m) for m in _ENDPOINTS}
        empty = [m for m, rows in by_market.items() if not rows]
        if empty and len(empty) < len(_ENDPOINTS):
            # 한쪽 시장만 비는 건 이상 신호다 — 스크리너가 반쪽 시장만 훑게 된다.
            # (양쪽 다 비면 그냥 휴장일이라 로그를 남기지 않는다: 최근 거래일을
            #  찾느라 주말을 거슬러 올라갈 때마다 경고가 쌓이면 소음이 된다.)
            logger.warning("KRX %s: %s 데이터 없음(다른 시장은 있음)", date, ", ".join(empty))
        quotes: List[DailyQuote] = []
        for rows in by_market.values():
            quotes.extend(rows)

        with _guard:
            _cache[date] = (_time.monotonic(), quotes)
        return quotes
