"""계좌별 토큰 수명 관리. 동시 발급 병합과 인증 실패 후 발급 폭주 방지."""
from __future__ import annotations

import threading
import time
from datetime import datetime

from ..event_log import record_event
from ..market_clock import KST
from . import kiwoom_api as kw


class TokenManager:
    REFRESH_AHEAD = 60.0
    RETRY_DELAY = 30.0

    def __init__(self, appkey: str, secretkey: str, mock: bool) -> None:
        self._appkey = appkey
        self._secretkey = secretkey
        self._mock = mock
        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = 0.0
        self._retry_at = 0.0
        self._refresh_after = 0.0
        self._rejected = False
        self.account = ""
        self.state = "pending"
        self.expires_dt = ""

    def get_token(self) -> str:
        with self._lock:
            now = time.time()
            valid = bool(self._token) and not self._rejected and now < self._expires_at
            if valid and (now < self._expires_at - self.REFRESH_AHEAD or now < self._refresh_after):
                return self._token
            if time.monotonic() < self._retry_at:
                raise kw.KiwoomAuthError("키움 인증 재시도 대기 중")
            self.state = "refreshing"
            try:
                grant = kw.fetch_access_token(self._appkey, self._secretkey, self._mock)
                expires = datetime.strptime(grant.expires_dt, "%Y%m%d%H%M%S").replace(tzinfo=KST)
                if not grant.value or expires.timestamp() <= time.time():
                    raise kw.KiwoomAuthError("토큰 유효기간 부족")
                self._token = grant.value
                self._expires_at = expires.timestamp()
                self.expires_dt = grant.expires_dt
                self._rejected = False
                # 발급 서버가 아직 유효한 같은 토큰을 반환해도 발급 요청이 몰리지 않게 한다.
                self._refresh_after = min(self._expires_at, time.time() + self.RETRY_DELAY)
                self.state = "ok"
                record_event("키움 인증 갱신 완료", event="broker_auth", account=self.account,
                             state=self.state, expires_dt=self.expires_dt)
                return self._token
            except Exception as exc:
                self.state = "error"
                self._retry_at = time.monotonic() + self.RETRY_DELAY
                record_event("키움 인증 갱신 실패", event="broker_auth", account=self.account,
                             state=self.state, error_type=type(exc).__name__)
                if valid and time.time() < self._expires_at:
                    self.state = "refreshing"
                    self._refresh_after = min(self._expires_at, time.time() + self.RETRY_DELAY)
                    return self._token
                raise kw.KiwoomAuthError("키움 인증 갱신 실패 — 자동 재시도 예정") from None

    def reject(self, used_token: str) -> None:
        with self._lock:
            # 늦게 도착한 이전 토큰의 실패로 새 토큰을 무효화하지 않는다.
            if used_token == self._token:
                if not self._rejected:
                    record_event("키움 토큰 인증 거부", event="broker_auth",
                                 account=self.account, state="error")
                self._rejected = True
                self.state = "error"

    def defer_retry(self) -> None:
        with self._lock:
            self._retry_at = time.monotonic() + self.RETRY_DELAY

    def status(self) -> str:
        if self.state in {"ok", "refreshing"} and time.time() >= self._expires_at:
            return "expired"
        return self.state
