"""데이터/브로커 제공자.

계좌 설정(AccountConfig)에 따라 구현체를 선택한다.
"""
from __future__ import annotations

from ..accounts import AccountConfig
from .base import Broker, DataProvider


def build_provider(cfg: AccountConfig) -> tuple[DataProvider, Broker]:
    """계좌 설정에 맞는 (DataProvider, Broker) 쌍을 생성."""
    if cfg.provider == "kiwoom":
        from .kiwoom import KiwoomBroker, KiwoomDataProvider

        dp = KiwoomDataProvider(
            appkey=cfg.appkey, secretkey=cfg.secret, mock=cfg.kiwoom_mock,
        )
        return dp, KiwoomBroker(dp)

    from .mock import MockBroker, MockDataProvider

    dp = MockDataProvider()
    return dp, MockBroker(dp)
