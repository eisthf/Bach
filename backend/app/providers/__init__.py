"""데이터/브로커 제공자.

PROVIDER 환경변수(mock|kiwoom)에 따라 구현체를 선택한다.
"""
from __future__ import annotations

import os

from .base import Broker, DataProvider


def build_provider() -> tuple[DataProvider, Broker]:
    """환경변수 PROVIDER 에 맞는 (DataProvider, Broker) 쌍을 생성."""
    provider = (os.getenv("PROVIDER") or "mock").strip().lower()
    if provider == "kiwoom":
        from .kiwoom import KiwoomBroker, KiwoomDataProvider

        dp = KiwoomDataProvider()
        return dp, KiwoomBroker(dp)

    from .mock import MockBroker, MockDataProvider

    dp = MockDataProvider()
    return dp, MockBroker(dp)
