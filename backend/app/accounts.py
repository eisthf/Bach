"""계좌(account) 설정 — multi-account 지원.

Bach는 여러 키움 계좌를 동시에 운용한다(예: 모의투자 + 실전).
계좌마다 별도의 앱키/시크릿/호스트를 갖는 DataProvider+Broker 쌍이 생성된다.

활성 계좌는 환경변수 ``ACCOUNTS`` (쉼표구분, 기본 ``mock``)로 선택한다.
계좌 id별 자격증명은 prefix 환경변수로 읽는다:

  ACCOUNTS=mock,real

  # 모의투자 계좌(mock) — 호스트 mockapi.kiwoom.com
  MOCK_PROVIDER=kiwoom        # kiwoom | mock(합성, 무자격증명)
  MOCK_APPKEY=...
  MOCK_SECRETKEY=...

  # 실전 계좌(real) — 호스트 api.kiwoom.com (위험!)
  REAL_PROVIDER=kiwoom
  REAL_APPKEY=...
  REAL_SECRETKEY=...

하위호환: ``mock`` 계좌는 자격증명이 없으면 구(舊) 단일계좌 변수
``PROVIDER``/``APPKEY``/``SECRETKEY`` 를 fallback으로 읽는다.

kiwoom 계좌인데 자격증명이 없으면 그 계좌는 건너뛴다(데모는 mock provider로
계속 동작). 활성 계좌가 하나도 안 남으면 합성 mock 계좌 1개로 대체한다.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger("bach.accounts")


@dataclass
class AccountConfig:
    id: str               # "mock" | "real" | ...
    label: str            # 화면 표시명 ("모의투자" / "실전")
    provider: str         # "kiwoom" | "mock"
    appkey: Optional[str]
    secret: Optional[str]
    kiwoom_mock: bool     # 키움 호스트: True=mockapi(모의) / False=api(실전)
    danger: bool          # 실거래(실전) → 프런트에서 위험 표시/확인 가드


def _account_config(aid: str) -> AccountConfig:
    """계좌 id 하나의 설정을 환경변수에서 조립."""
    up = aid.upper()
    provider = (os.getenv(f"{up}_PROVIDER") or "").strip().lower()
    appkey = os.getenv(f"{up}_APPKEY")
    secret = os.getenv(f"{up}_SECRETKEY")

    if aid == "mock":
        # 하위호환: 구 단일계좌 변수 fallback
        provider = provider or (os.getenv("PROVIDER") or "mock").strip().lower()
        appkey = appkey or os.getenv("APPKEY")
        secret = secret or os.getenv("SECRETKEY")
        label = "모의투자"
        kiwoom_mock = True       # 모의 계좌는 항상 mockapi 호스트
        danger = False
    elif aid == "real":
        provider = provider or "kiwoom"
        label = "실전"
        kiwoom_mock = False      # 실전 계좌는 api(실전) 호스트
        danger = True
    else:
        # 일반 계좌: prefix 변수로만 구성
        provider = provider or "mock"
        label = os.getenv(f"{up}_LABEL") or aid
        kiwoom_mock = (os.getenv(f"{up}_KIWOOM_MOCK", "true").strip().lower() == "true")
        danger = (os.getenv(f"{up}_DANGER", "false").strip().lower() == "true")

    return AccountConfig(
        id=aid, label=label, provider=provider,
        appkey=appkey, secret=secret, kiwoom_mock=kiwoom_mock, danger=danger,
    )


def load_account_configs() -> List[AccountConfig]:
    """활성 계좌 설정 목록을 반환(ACCOUNTS 순서 유지)."""
    ids = [a.strip() for a in (os.getenv("ACCOUNTS") or "mock").split(",") if a.strip()]
    out: List[AccountConfig] = []
    seen = set()
    for aid in ids:
        if aid in seen:
            continue
        seen.add(aid)
        cfg = _account_config(aid)
        if cfg.provider == "kiwoom" and (not cfg.appkey or not cfg.secret):
            logger.warning(
                "계좌 '%s'(%s): kiwoom 자격증명(%s_APPKEY/SECRETKEY) 없음 → 건너뜀",
                aid, cfg.label, aid.upper(),
            )
            continue
        out.append(cfg)
    if not out:
        logger.warning("활성 계좌 없음 → 합성 mock 계좌로 대체")
        out.append(AccountConfig(
            id="mock", label="모의투자(데모)", provider="mock",
            appkey=None, secret=None, kiwoom_mock=True, danger=False,
        ))
    return out
