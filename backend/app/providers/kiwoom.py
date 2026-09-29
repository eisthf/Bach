"""키움 실거래 어댑터 (PROVIDER=kiwoom 일 때만 활성).

self-contained `kiwoom_api` 클라이언트만 사용한다(외부 kiwoom 프로젝트 의존 없음).
mock 모드에서는 이 모듈이 import 되지 않으므로 키움 의존성 없이 앱이 뜬다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from datetime import time as dtime
from typing import AsyncIterator, Callable, Dict, List, Optional

from ..event_log import record_event
from ..market_clock import chart_epoch, now_kst, REGULAR_OPEN, REGULAR_CLOSE
from ..timing import mark
from ..models import Bar, OrderResult, Position, Tick
from . import kiwoom_api as kw
from .base import Broker, DAY_INTERVAL, DataProvider
from .kiwoom_auth import TokenManager

logger = logging.getLogger("bach.kiwoom")

# 0B(주식체결) 실시간 FID 맵 — 함정: 거래량은 15(체결량)/13(누적), 11이 아님.
F_PRICE = "10"   # 현재가(부호 포함)
F_OPEN = "16"    # 시가
F_HIGH = "17"    # 고가(당일)
F_LOW = "18"     # 저가(당일)
F_VOL = "15"     # 체결량
F_AMOUNT = "14"  # 누적거래대금(백만원)

# 00(주문체결) 실시간 FID 맵 (레퍼런스 검증)
#  함정: 체결가/체결량은 '단위'(이번 분) 914/915. 911(체결량)은 원주문 누적이라 사용 금지.
FILL_CODE = "9001"     # 종목코드
FILL_STATUS = "913"    # 주문상태: 접수/체결/확인/취소/거부
FILL_PRICE = "914"     # 단위체결가(이번 체결분)
FILL_QTY = "915"       # 단위체결량(이번 체결분)
FILL_UNFILLED = "902"  # 미체결수량
FILL_SIDE = "905"      # 주문구분: +매수 / -매도
FILL_ORDNO = "9203"    # 주문번호


def _today() -> str:
    return now_kst().strftime("%Y%m%d")


def _minute_of(epoch: int) -> int:
    """차트 epoch(KST 벽시계를 UTC로 저장)의 하루 중 분."""
    dt = datetime.fromtimestamp(epoch, timezone.utc)
    return dt.hour * 60 + dt.minute


# 시가(Z) 검증 진단 구간. 장 시작 직후 걸러진 0B 틱의 사유를 남겨 Z 확보 지연이
# 거래소 쪽(랜덤엔드·VI로 첫 체결이 늦음)인지 우리 필터 쪽인지 가린다.
OPEN_DIAG_START = dtime(8, 59, 30)
OPEN_DIAG_END = dtime(9, 3)
OPEN_DIAG_SAMPLES = 5  # 종목·일별로 원본 필드를 남길 탈락 틱 수
OPEN_DIAG_FIELDS = ("10", "16", "15", "20", "290", "9081")


def _open_rejections(v: dict, open_: float, now) -> list[str]:
    """0B 틱이 당일 KRX 정규장 시가로 검증되지 못한 사유. 빈 목록이면 통과."""
    reasons = []
    local = now.strftime("%H%M%S")
    trade_time = str(v.get("20") or "")  # 체결시간 HHMMSS
    if open_ <= 0:
        reasons.append("open_zero")
    if now.weekday() >= 5:
        reasons.append("weekend")
    if not (REGULAR_OPEN <= now.time() <= REGULAR_CLOSE) or local > "153000":
        reasons.append("local_outside_session")
    if len(trade_time) != 6 or not trade_time.isdigit():
        reasons.append("trade_time_invalid")
    else:
        if trade_time < "090000":
            reasons.append("trade_time_before_open")
        if trade_time > local:
            reasons.append("trade_time_ahead_of_local")
    if str(v.get("290")) != "2":
        reasons.append(f"session_{v.get('290')}")
    if v.get("9081") != "KRX":
        reasons.append(f"exchange_{v.get('9081')}")
    if kw.parse_price(v.get(F_VOL)) <= 0:
        reasons.append("volume_zero")
    return reasons


class KiwoomDataProvider(DataProvider):
    # 봉 캐시 유효시간(초). 동시·연속 요청을 합치는 것이 목적이라 짧게 잡는다.
    _BARS_TTL = 3.0

    def __init__(
        self,
        appkey: Optional[str] = None,
        secretkey: Optional[str] = None,
        mock: Optional[bool] = None,
    ) -> None:
        # 계좌별로 인자 주입. 미지정 시 구 단일계좌 환경변수로 fallback.
        appkey = appkey or os.getenv("APPKEY")
        secretkey = secretkey or os.getenv("SECRETKEY")
        if mock is None:
            mock = (os.getenv("KIWOOM_MOCK", "true").lower() == "true")
        self._mock = mock
        if not appkey or not secretkey:
            raise RuntimeError(
                "kiwoom provider 인데 APPKEY/SECRETKEY 가 없습니다(.env 확인)."
            )
        self._auth = TokenManager(appkey, secretkey, self._mock)
        self._rest_state = "pending"
        self._ws_state = "idle"
        self._last_received_at = None
        self._ws_token = ""
        self._auth_task: Optional[asyncio.Task] = None
        self._last: Dict[str, Tick] = {}
        self._day_open: Dict[str, float] = {}    # 당일 시가(Z) 캐시
        self._day_open_day: Dict[str, str] = {}
        self._open_received_ns: Dict[str, int] = {}
        self._open_waiters: Dict[str, asyncio.Event] = {}  # 시가 대기 중인 셋업 깨우기
        self._open_diag: Dict[str, dict] = {}    # 장 시작 직후 시가 검증 진단
        self._prev_close: Dict[str, float] = {}  # 전일 종가(X) 캐시
        self._prev_close_day: Dict[str, str] = {}
        self._limits: Dict[str, tuple] = {}      # 당일 가격제한 캐시: code -> (날짜, dict)
        self._name: Dict[str, str] = {}          # 종목명 캐시
        self._etp: set = set()                   # ETF·ETN 코드(거래대금 양봉 필터용)
        self._etp_day = ""
        # 봉 조회 단기 캐시: {(code, interval, lookback, bucket): (조회시각, bars)}
        self._bars_cache: Dict[tuple, tuple] = {}
        self._bars_locks: Dict[tuple, threading.Lock] = {}
        self._bars_guard = threading.Lock()
        # 실시간 0B: 키움은 앱키당 WebSocket 1개만 허용 → 단일 연결로 전 종목을
        # 멀티플렉싱한다(종목마다 연결하면 서로 LOGIN으로 밀어내 끊김 반복).
        self._queues: Dict[str, "asyncio.Queue[Tick]"] = {}  # code -> 소비 큐
        self._subscribed: set[str] = set()       # 구독 중인 종목코드
        self._ws = None                           # 활성 websocket(없으면 None)
        self._ws_task: Optional[asyncio.Task] = None
        # 주문체결(00) 이벤트 콜백. Hub가 설정한다. fill dict 또는 None(전체 갱신).
        self.on_order_fill: Optional[Callable[[Optional[dict]], None]] = None

    def connection_status(self) -> dict:
        return {"auth": self._auth.status(), "rest": self._rest_state,
                "stream": self._ws_state, "last_received_at": self._last_received_at}

    def set_account(self, account: str) -> None:
        self._auth.account = account

    def start(self) -> None:
        self._ensure_ws()

    def _call(self, fn, *args, retry_auth: bool = True, **kwargs):
        """조회만 인증 복구 후 한 번 재시도. 주문은 재전송하지 않는다."""
        for attempt in range(2 if retry_auth else 1):
            try:
                token = self._auth.get_token()
            except kw.KiwoomAuthError:
                self._rest_state = "error"
                raise
            try:
                result = fn(token, *args, **kwargs)
                self._rest_state = "ok" if result is not None else "error"
                return result
            except kw.KiwoomAuthError:
                self._rest_state = "error"
                self._auth.reject(token)
                if attempt or not retry_auth:
                    self._auth.defer_retry()
                    raise
            except Exception:
                self._rest_state = "error"
                raise

    async def _maintain_auth(self) -> None:
        while True:
            token = None
            try:
                token = await asyncio.to_thread(self._auth.get_token)
            except kw.KiwoomAuthError:
                self._ws_state = "auth_error"
            try:
                if self._ws is not None and token != self._ws_token:
                    await self._ws.close()
            except Exception as exc:
                logger.warning("키움 연결 유지 재시도: %s", type(exc).__name__)
            await asyncio.sleep(15)

    async def close(self) -> None:
        tasks = [t for t in (self._ws_task, self._auth_task) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._ws = None
        self._ws_state = "disconnected"

    # -- 봉 --------------------------------------------------------------
    def get_bars(self, code: str, interval: int, lookback_extra: int = 60) -> List[Bar]:
        """분봉·일봉 조회. 같은 요청이 몰리면 합친다(단기 캐시 + 동시요청 병합).

        같은 (종목,간격)을 여러 곳이 거의 동시에 요청한다: 차트 카드 여러 개가
        한꺼번에 마운트될 때, 브라우저 탭이 여러 개일 때, 그리고 봉 경계에서
        모든 차트가 동시에 재조회할 때. ka10080 은 페이징까지 하는데 REST 게이트가
        0.35초 간격으로 직렬화하므로, 중복이 그대로 지연으로 쌓인다.

        캐시 키에 '현재 봉 버킷'을 포함해, 봉 경계를 넘는 순간 자동으로 무효화된다
        (경계 직전 응답이 재사용되어 새 봉이 안 보이는 일이 없다). 버킷 안에서는
        TTL 만큼 재사용하는데, 진행 중인 봉의 종가는 프런트가 틱으로 갱신하므로
        몇 초 묵어도 화면상 차이가 없다.

        키마다 락을 잡아 동시 요청을 하나로 합친다(뒤따라온 호출은 앞선 조회가
        끝난 뒤 그 결과를 재사용). 서로 다른 키는 막지 않는다.
        """
        bucket = chart_epoch() // max(1, interval * 60)
        key = (code, interval, lookback_extra, bucket)
        with self._bars_lock(key):
            hit = self._bars_cache.get(key)
            if hit is not None:
                age = time.time() - hit[0]
                # 오늘 진행 일봉까지 받은 응답은 자정까지 재사용한다. 장중
                # 고가·저가·종가는 실시간 틱이 프런트의 마지막 봉을 갱신한다.
                has_today = (
                    interval == DAY_INTERVAL
                    and hit[1]
                    and hit[1][-1].time // (DAY_INTERVAL * 60) == bucket
                )
                if has_today or age < self._BARS_TTL:
                    return list(hit[1])
            bars = self._fetch_bars(code, interval, lookback_extra)
            now = time.time()
            self._bars_cache[key] = (now, bars)
            # 버킷이 바뀌면 옛 키는 다시 쓰이지 않으므로 주기적으로 정리.
            current_day_bucket = chart_epoch() // (DAY_INTERVAL * 60)
            for k, (ts, cached_bars) in list(self._bars_cache.items()):
                current_daily = (
                    k[1] == DAY_INTERVAL
                    and k[3] == current_day_bucket
                    and cached_bars
                    and cached_bars[-1].time // (DAY_INTERVAL * 60) == current_day_bucket
                )
                if not current_daily and now - ts > 60:
                    self._bars_cache.pop(k, None)
                    self._bars_locks.pop(k, None)
            return list(bars)

    def _bars_lock(self, key: tuple) -> threading.Lock:
        with self._bars_guard:
            lk = self._bars_locks.get(key)
            if lk is None:
                lk = self._bars_locks[key] = threading.Lock()
            return lk

    def _fetch_bars(self, code: str, interval: int, lookback_extra: int) -> List[Bar]:
        # rate-limit 등으로 빈 응답이 오면 짧게 쉬고 1회 재시도(빈 차트 방지).
        rows: List[dict] = []
        for attempt in range(2):
            if interval == DAY_INTERVAL:
                rows = self._call(
                    kw.fetch_day_bars, code, mock=self._mock,
                    today=_today(), lookback_extra=lookback_extra,
                )
            else:
                rows = self._call(
                    kw.fetch_min_bars, code, interval, mock=self._mock,
                    lookback_extra=lookback_extra,
                )
            if rows:
                break
            if attempt == 0:
                label = "일봉" if interval == DAY_INTERVAL else "분봉"
                logger.warning("[%s] %s 빈 응답 → 재시도", code, label)
                time.sleep(0.3)
        bars: List[Bar] = []
        for r in rows:
            bars.append(Bar(
                time=r["time"], open=r["open"], high=r["high"],
                low=r["low"], close=r["close"], volume=r["volume"],
                amount=r.get("amount", 0.0),
            ))
        return bars

    # -- X/Z (자동매매 엔진 셋업용) ---------------------------------------
    def prev_close(self, code: str) -> Optional[float]:
        today = _today()
        if self._prev_close_day.get(code) == today:
            return self._prev_close.get(code)
        x = self._call(kw.fetch_prev_close, code, mock=self._mock, today=today)
        if x:
            self._prev_close[code] = x
            self._prev_close_day[code] = today
        return x

    def day_open(self, code: str) -> Optional[float]:
        """검증된 당일 KRX 정규장 체결의 시가만 반환한다. 네트워크 호출 없음."""
        now = now_kst()
        if now.weekday() >= 5 or now.time() < REGULAR_OPEN:
            return None
        if self._day_open_day.get(code) == now.strftime("%Y%m%d"):
            return self._day_open[code]
        return None

    def open_received_ns(self, code: str) -> int:
        return self._open_received_ns.get(code, 0) if self._day_open_day.get(code) == _today() else 0

    async def wait_day_open(self, code: str, timeout: float) -> None:
        """검증된 당일 첫 체결이 들어오는 즉시 깨어난다(폴링 간격만큼 늦지 않게)."""
        if self.day_open(code) is not None:
            return
        waiter = self._open_waiters.get(code)
        if waiter is None:
            waiter = self._open_waiters[code] = asyncio.Event()
        try:
            await asyncio.wait_for(waiter.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    def open_diagnostics(self, code: str) -> dict:
        diag = self._open_diag.get(code)
        if not diag or diag["day"] != _today():
            return {}
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in diag.items() if k != "samples"}

    def _diagnose_open(self, code: str, now, v: dict, rejections: list[str]) -> None:
        """시가 확보 전 틱을 집계하고, 탈락 샘플과 첫 검증 통과를 이벤트 로그에 남긴다."""
        today = now.strftime("%Y%m%d")
        stamp = now.strftime("%H:%M:%S.%f")[:-3]
        diag = self._open_diag.get(code)
        if diag is None or diag["day"] != today:
            diag = self._open_diag[code] = {"day": today, "first_0b_at": stamp,
                                            "ticks": 0, "rejected": {}, "samples": 0}
        diag["ticks"] += 1
        fields = {k: v.get(k) for k in OPEN_DIAG_FIELDS}
        account = getattr(self._auth, "account", "")
        if rejections:
            for reason in rejections:
                diag["rejected"][reason] = diag["rejected"].get(reason, 0) + 1
            if diag["samples"] < OPEN_DIAG_SAMPLES:
                diag["samples"] += 1
                record_event(f"[{code}] 시가 검증 탈락 틱: {', '.join(rejections)}",
                             event="open_tick_rejected", account=account, code=code,
                             local_time=stamp, reasons=rejections, fields=fields)
            return
        diag["verified_at"] = stamp
        record_event(
            f"[{code}] 시가 검증 통과: 첫 0B {diag['first_0b_at']} → 검증 {stamp} "
            f"(앞선 탈락 {diag['ticks'] - 1}건)",
            event="open_verified", account=account, code=code,
            first_0b_at=diag["first_0b_at"], verified_at=stamp,
            rejected_ticks=diag["ticks"] - 1, rejected=dict(diag["rejected"]), fields=fields)

    def stock_name(self, code: str, refresh: bool = False) -> Optional[str]:
        # 종목 추가 시 1회: 종목명 + 초기 시세(현재가/시고저)를 ka10007로 시드.
        # 거래가 드문 종목은 첫 0B 틱까지 시간이 걸려 헤더가 '-'로 보이는데,
        # 이 시드로 추가 즉시 값이 표시되고 이후 실시간 틱이 갱신한다.
        if refresh or code not in self._name:
            before = self._last.get(code)
            q = self._call(kw.fetch_quote, code, mock=self._mock)
            if q:
                if q.get("name"):
                    self._name[code] = q["name"]
                price = q.get("price") or 0.0
                if price > 0 and (before is None or refresh) and self._last.get(code) is before:
                    op = q.get("open") or price
                    self._last[code] = Tick(
                        code=code, price=price,
                        high=q.get("high") or price, low=q.get("low") or price,
                        open=op, volume=0.0, time=chart_epoch(),
                    )
                    # 날짜 없는 시세표는 장전에 전날 값을 반환할 수 있다.
                    # 화면 초기 표시용일 뿐 자동매매 시가 캐시에는 넣지 않는다.
        return self._name.get(code)

    def integrated_code(self, code: str) -> str:
        return f"{code}_AL"

    def price_limits(self, code: str) -> Optional[dict]:
        """당일 기준가·상한가(ka10001). 하루 동안 변하지 않으므로 날짜별로 캐시."""
        today = _today()
        cached = self._limits.get(code)
        if cached and cached[0] == today:
            return cached[1]
        limits = self._call(kw.fetch_price_limits, code, mock=self._mock)
        if limits:
            self._limits[code] = (today, limits)
        return limits

    def last_price(self, code: str) -> Optional[float]:
        """현재가(ka10007) — 시간외 체결까지 반영된 마지막 거래 가격."""
        quote = self._call(kw.fetch_quote, code, mock=self._mock)
        return (quote.get("price") or None) if quote else None

    def current_upper_limits(self, krx_only: bool = False) -> list[dict] | None:
        """키움 ka10017(기본 통합, ``krx_only``면 KRX)로 당일 상한가 종목을 조회한다.

        정규장 마감 후엔 현재가에 시간외 체결이 섞이므로, 종목마다 정규장 종가
        (KRX 3분봉의 15:30 종가 단일가 봉)를 ``regular_close``로 붙인다.
        """
        rows = self._call(kw.fetch_upper_limits, mock=self._mock,
                          exchange="1" if krx_only else "3")
        if rows is None:
            return None
        now = now_kst()
        if now.weekday() < 5 and now.time() >= REGULAR_CLOSE:
            for row in rows:
                row["regular_close"] = self._regular_close(row["code"], now)
        return rows

    def _regular_close(self, code: str, now) -> int:
        """오늘 정규장 종가(15:30 봉까지의 마지막 봉 종가). 모르면 0."""
        try:
            bars = self.get_bars(code, 3, 0)
        except Exception:  # noqa: BLE001 — 구분 실패가 목록 조회를 막지 않게
            logger.warning("[%s] 정규장 종가 조회 실패", code, exc_info=True)
            return 0
        today = now.date()
        regular = [b for b in bars
                   if datetime.fromtimestamp(b.time, timezone.utc).date() == today
                   and 540 <= _minute_of(b.time) <= 930]
        return int(regular[-1].close) if regular else 0

    def current_big_candles(self, min_amount_krw: int) -> dict | None:
        """키움 ka10032+ka10028로 당일 거래대금 상위 양봉을 조회한다.

        종목마다 ``etp``(ETF·ETN 여부)를 붙인다. ETF·ETN 목록을 못 얻으면
        ``etp_known=False``로 알려 화면이 필터를 적용하지 못했음을 표시하게 한다.
        """
        result = self._call(kw.fetch_big_candles, min_amount_krw, mock=self._mock)
        if result is None:
            return None
        etp = self._etp_codes()
        result["etp_known"] = etp is not None
        for item in result.get("stocks") or []:
            item["etp"] = bool(etp) and item.get("code") in etp
        return result

    def _etp_codes(self) -> Optional[set]:
        """ETF·ETN 코드(하루 1회 조회 후 캐시). 실패는 캐시하지 않고 다음 조회에 재시도."""
        today = _today()
        if self._etp_day != today:
            try:
                codes = self._call(kw.fetch_etp_codes, mock=self._mock)
            except Exception:  # noqa: BLE001 — 구분 실패가 목록 조회를 막지 않게
                logger.warning("ETF·ETN 목록 조회 실패", exc_info=True)
                codes = None
            if codes is None:
                return None
            self._etp, self._etp_day = codes, today
        return self._etp

    # -- 틱 --------------------------------------------------------------
    def last_tick(self, code: str) -> Optional[Tick]:
        return self._last.get(code)

    async def stream_ticks(self, code: str) -> AsyncIterator[Tick]:
        """종목별 틱 스트림. 내부적으로 단일 공유 WS에서 0B를 라우팅한다."""
        q: "asyncio.Queue[Tick]" = asyncio.Queue(maxsize=1000)
        self._queues[code] = q
        self._subscribed.add(code)
        self._ensure_ws()
        await self._register()  # 연결돼 있으면 전체 구독을 즉시 REG
        try:
            while True:
                yield await q.get()
        finally:
            self._queues.pop(code, None)
            self._subscribed.discard(code)
            await self._register()

    # -- 단일 공유 WebSocket 관리 -----------------------------------------
    def _ensure_ws(self) -> None:
        if self._auth_task is None or self._auth_task.done():
            self._auth_task = asyncio.create_task(self._maintain_auth())
        if self._ws_task is None or self._ws_task.done():
            self._ws_task = asyncio.create_task(self._run_ws())

    async def _register(self) -> None:
        """연결돼 있으면 현재 구독 중인 전체 종목을 한 REG로 등록.

        refresh '1'이 grp_no를 갱신(치환)할 수 있으므로, 증분이 아니라 항상
        전체 집합을 보낸다(일부만 보내면 나머지 구독이 풀릴 수 있음).
        """
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps(self._registration()))
        except Exception:  # noqa: BLE001
            pass  # 다음 재연결 시 일괄 등록됨

    def _registration(self) -> dict:
        data = [{"item": [], "type": ["00"]}]
        if self._subscribed:
            data.insert(0, {"item": list(self._subscribed), "type": ["0B"]})
        return {"trnm": "REG", "grp_no": "1", "refresh": "1", "data": data}

    async def _run_ws(self) -> None:
        """앱키당 1개만 허용되는 실시간 소켓. 전 종목을 한 연결로 멀티플렉싱."""
        import websockets  # 지연 import (mock 모드 무의존)

        uri = kw.ws_url(self._mock)
        backoff = 1.0
        while True:
            try:
                token = await asyncio.to_thread(self._auth.get_token)
                self._ws_state = "connecting"
                async with websockets.connect(uri, ping_interval=None) as ws:
                    await ws.send(json.dumps({"trnm": "LOGIN", "token": token}))
                    async for raw in ws:
                        received_ns = time.perf_counter_ns()
                        msg = json.loads(raw)
                        trnm = msg.get("trnm")
                        if trnm == "LOGIN":
                            if msg.get("return_code") != 0:
                                self._ws_state = "auth_error"
                                if kw.is_auth_error(msg):
                                    self._auth.reject(token)
                                logger.error("키움 WS 로그인 거부")
                                break
                            backoff = 1.0
                            self._ws = ws
                            self._ws_token = token
                            # 현재 구독 중인 전 종목(0B) + 주문체결(00)을 한 번에 등록
                            await ws.send(json.dumps(self._registration()))
                            self._ws_state = "connected"
                            logger.info("WS 등록 0B=%s + 00(주문체결)",
                                        ", ".join(self._subscribed) or "(없음)")
                            # 재접속 시 누락된 체결 보정: 전체 포지션 갱신 요청
                            if self.on_order_fill:
                                self.on_order_fill(None)
                        elif trnm == "PING":
                            await ws.send(raw)  # echo (keepalive)
                        elif trnm == "REAL":
                            self._dispatch_real(msg, received_ns)
            except asyncio.CancelledError:
                self._ws = None
                raise
            except kw.KiwoomAuthError:
                self._ws_state = "auth_error"
            except Exception as e:  # noqa: BLE001
                logger.warning("WS 재연결 (%.0fs 후): %s", backoff, type(e).__name__)
            self._ws = None
            if self._ws_state != "auth_error":
                self._ws_state = "disconnected"
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _dispatch_real(self, msg: dict, received_ns: int | None = None) -> None:
        """REAL 메시지를 라우팅: 0B→틱 큐, 00→주문체결 콜백."""
        received_ns = received_ns if received_ns is not None else time.perf_counter_ns()
        for d in msg.get("data", []):
            dtype = d.get("type")
            if dtype == "00":
                self._handle_fill(d.get("values", {}))
                continue
            if dtype != "0B":
                continue
            code = d.get("item") or ""
            if code not in self._queues:
                continue
            v = d.get("values", {})
            price = kw.parse_price(v.get(F_PRICE))
            if price <= 0:
                continue
            open_ = kw.parse_price(v.get(F_OPEN))
            now = now_kst()
            rejections = _open_rejections(v, open_, now)
            verified = not rejections
            first_today = self._day_open_day.get(code) != now.strftime("%Y%m%d")
            if (first_today and now.weekday() < 5
                    and OPEN_DIAG_START <= now.time() <= OPEN_DIAG_END):
                try:
                    self._diagnose_open(code, now, v, rejections)
                except Exception:  # 진단 실패가 시세 처리를 막지 않게 격리
                    logger.exception("시가 진단 기록 실패")
            if verified:
                if first_today:
                    self._open_received_ns[code] = received_ns
                self._day_open[code] = open_
                self._day_open_day[code] = now.strftime("%Y%m%d")
                if first_today:
                    waiter = self._open_waiters.pop(code, None)
                    if waiter is not None:
                        waiter.set()
            tick = Tick(
                code=code,
                price=price,
                high=kw.parse_price(v.get(F_HIGH)) or price,
                low=kw.parse_price(v.get(F_LOW)) or price,
                open=open_, open_verified=verified, received_ns=received_ns,
                volume=kw.parse_price(v.get(F_VOL)),
                amount=kw.parse_price(v.get(F_AMOUNT)) * 1_000_000,
                time=chart_epoch(),
            )
            self._last[code] = tick
            self._last_received_at = now_kst().isoformat()
            q = self._queues.get(code)
            if q is not None:
                try:
                    q.put_nowait(tick)
                except asyncio.QueueFull:
                    pass  # 소비가 느리면 최신 틱 일부 드롭(허용)

    def _handle_fill(self, v: dict) -> None:
        """00(주문체결) → 체결분만 골라 콜백 통지(평단/보유는 Hub가 계좌로 갱신)."""
        if v.get(FILL_STATUS) != "체결":
            return  # 접수/확인/취소/거부는 무시(체결만 반영)
        code = str(v.get(FILL_CODE, "")).lstrip("A").strip()
        if not code or self.on_order_fill is None:
            return
        side = "buy" if "+" in str(v.get(FILL_SIDE, "")) else "sell"
        fill = {
            "code": code,
            "side": side,
            "qty": kw.parse_int(v.get(FILL_QTY)),
            "price": kw.parse_price(v.get(FILL_PRICE)),
            "unfilled": kw.parse_int(v.get(FILL_UNFILLED)),
            "order_no": str(v.get(FILL_ORDNO, "")).strip(),
        }
        # 공식 00 응답: 908=체결시각(HHmmss), 909=체결번호.
        received = now_kst()
        executed = received
        source = "received"
        clock = str(v.get("908") or "").strip()
        if len(clock) == 6 and clock.isdigit():
            try:
                executed = received.replace(hour=int(clock[:2]), minute=int(clock[2:4]), second=int(clock[4:]))
                source = "execution"
            except ValueError:
                pass
        execution_no = str(v.get("909") or "").strip()
        identity = f"{executed.date()}:{fill['order_no']}:{execution_no}" if execution_no and fill["order_no"] else None
        self.record_trade(code, side, fill["qty"], fill["price"], time=chart_epoch(executed), time_source=source, identity=identity)
        try:
            self.on_order_fill(fill)
        except Exception:  # noqa: BLE001
            logger.exception("on_order_fill 콜백 오류")


class KiwoomBroker(Broker):
    """주문 실행 + 계좌 기반 포지션 조회.

    포지션은 키움 계좌(kt00018)를 단일 진실원으로 삼는다(짧은 TTL 캐시).
    """

    _POS_TTL = 2.0  # seconds

    def __init__(self, data: KiwoomDataProvider) -> None:
        self._data = data
        self._mock = data._mock
        self._pos_cache: Dict[str, dict] = {}
        self._pos_ts = 0.0

    def _price(self, code: str) -> float:
        t = self._data.last_tick(code)
        return t.price if t else 0.0

    def invalidate(self) -> None:
        self._pos_ts = 0.0

    def account_summary(self) -> Optional[dict]:
        return self._data._call(kw.fetch_account_summary, mock=self._mock)

    def unfilled_orders(self) -> dict:
        grouped: Dict[str, list] = {}
        for o in self._data._call(kw.fetch_unfilled, mock=self._mock):
            grouped.setdefault(o["code"], []).append(o)
        return grouped

    def _positions(self, force: bool = False) -> Dict[str, dict]:
        now = time.time()
        if force or now - self._pos_ts > self._POS_TTL:
            self._pos_cache = self._data._call(kw.fetch_positions, mock=self._mock)
            self._pos_ts = now
        return self._pos_cache

    def buy(self, code: str, amount_krw: int) -> OrderResult:
        mark("worker_start")
        price = self._price(code)
        qty = int(amount_krw // price) if price > 0 else 0
        if qty <= 0:
            return OrderResult(ok=False, code=code, side="buy", filled_qty=0,
                               price=price, message="현재가 없음 또는 금액 부족")
        try:
            order_no = self._data._call(kw.place_order, code, qty, "buy",
                                      mock=self._mock, order_type="3", retry_auth=False)
        except kw.KiwoomRequestError:
            return OrderResult(ok=False, code=code, side="buy", filled_qty=0,
                               price=price, message="주문 요청 실패 — 재주문 전 계좌 접수 내역을 확인하세요.")
        ok = order_no is not None
        if ok:
            self._pos_ts = 0.0  # 다음 조회 시 갱신
        return OrderResult(ok=ok, code=code, side="buy", filled_qty=qty if ok else 0,
                           price=price, order_no=order_no,
                           message=f"{qty}주 시장가 매수 전송" if ok else "주문 실패")

    def buy_ask3(self, code: str, amount_krw: int) -> OrderResult:
        try:
            price = self._data._call(kw.fetch_ask3, code, mock=self._mock)
        except (kw.KiwoomRequestError, kw.KiwoomAuthError):
            price = 0
        qty = int(amount_krw // price) if price > 0 else 0
        if qty <= 0:
            return OrderResult(ok=False, code=code, side="buy", filled_qty=0,
                               price=price, message="매도 3호가 없음 또는 금액 부족")
        try:
            order_no = self._data._call(kw.place_order, code, qty, "buy",
                mock=self._mock, order_type="0", price=str(int(price)), retry_auth=False)
        except (kw.KiwoomRequestError, kw.KiwoomAuthError):
            return OrderResult(ok=False, code=code, side="buy", filled_qty=0,
                               price=price, message="지정가 주문 요청 실패 — 접수 내역 확인 필요")
        ok = order_no is not None
        if ok:
            self._pos_ts = 0.0
        return OrderResult(ok=ok, code=code, side="buy", filled_qty=qty if ok else 0,
                           price=price, order_no=order_no,
                           message=f"{qty}주 매도 3호가 지정가 {price:,.0f}원 매수 전송" if ok else "주문 실패")

    def sell(self, code: str, qty: int) -> OrderResult:
        try:
            order_no = self._data._call(kw.place_order, code, qty, "sell",
                                      mock=self._mock, order_type="3", retry_auth=False)
        except kw.KiwoomRequestError:
            return OrderResult(ok=False, code=code, side="sell", filled_qty=0,
                               price=self._price(code), message="주문 요청 실패 — 재주문 전 계좌 접수 내역을 확인하세요.")
        ok = order_no is not None
        price = self._price(code)
        if ok:
            self._pos_ts = 0.0
        return OrderResult(ok=ok, code=code, side="sell", filled_qty=qty if ok else 0,
                           price=price, order_no=order_no,
                           message=f"{qty}주 시장가 매도 전송" if ok else "주문 실패")

    def position(self, code: str) -> Position:
        p = self._positions().get(code)
        if not p:
            return Position(code=code, current_price=self._price(code))
        return Position(
            code=code,
            quantity=p["quantity"],
            avg_price=p["avg_price"],
            current_price=self._price(code) or p["current_price"],
        )

    def all_positions(self) -> List[Position]:
        return [self.position(code) for code in self._positions(force=True)]
