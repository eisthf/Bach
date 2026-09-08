"""공용 테스트 픽스처.

mock provider 계좌의 Hub 를 실제로 조립하되, WS 버스는 FakeManager 로 대체해
브로드캐스트 메시지를 수집한다. 영속화(_persist)는 꺼서 state.json 을 건드리지
않는다.
"""
from __future__ import annotations

import pytest

from app.accounts import AccountConfig
from app.hub import Hub
from app.market_clock import MarketClock
from app.models import AutoConfig
from app.strategy.ulc import Phase, UlcEngine


class FakeManager:
    """AccountManager 대신 브로드캐스트만 수집."""

    def __init__(self) -> None:
        self.msgs: list[dict] = []

    def broadcast(self, msg: dict) -> None:
        self.msgs.append(msg)

    def logs(self, since: int = 0) -> list[str]:
        return [m["text"] for m in self.msgs[since:] if m.get("type") == "log"]


@pytest.fixture
def mock_hub():
    """합성 mock provider 계좌의 (hub, manager, clock)."""
    cfg = AccountConfig(id="mock", label="모의투자", provider="mock",
                        appkey=None, secret=None, kiwoom_mock=True, danger=False)
    clock = MarketClock(auto=False)
    mgr = FakeManager()
    hub = Hub(cfg, clock, mgr)
    hub._persist = lambda: None  # 테스트 중 state.json 쓰기 방지
    hub.AUTO_SETUP_TIMEOUT = 0.3
    hub.AUTO_SETUP_INTERVAL = 0.01
    yield hub, mgr, clock
    for stock in hub.stocks.values():  # 틱 태스크 정리
        if stock.setup_task:
            stock.setup_task.cancel()
        if stock.task:
            stock.task.cancel()


def make_engine(account=None, logs: list | None = None, *,
                x: float = 10_000.0, z: float = 10_000.0) -> UlcEngine:
    """계좌 응답을 고정한 단독 엔진. account = (qty, avg) 또는 None."""

    async def position_fn():
        return account

    eng = UlcEngine(code="005930", config=AutoConfig(), x=x, z=z,
                    log=(logs.append if logs is not None else (lambda m: None)),
                    position_fn=position_fn)
    eng.phase = Phase.ACCUMULATING
    return eng
