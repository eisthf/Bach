"""공용 테스트 픽스처.

mock provider 계좌의 Hub 를 실제로 조립하되, WS 버스는 FakeManager 로 대체해
브로드캐스트 메시지를 수집한다. 영속화(_persist)는 꺼서 state.json 을 건드리지
않는다.
"""
from __future__ import annotations

import os
import tempfile

# app.main 은 import 시점에 이벤트 로그 파일을 연다(configure_event_log). 테스트가
# 운영 로그(backend/logs/events.log)에 섞이지 않도록 app 을 import 하기 전에
# 임시 디렉터리로 돌린다. 개발자가 설정한 BACH_LOG_DIR 도 테스트에서는 덮어쓴다.
os.environ["BACH_LOG_DIR"] = tempfile.mkdtemp(prefix="bach-test-logs-")

import pytest  # noqa: E402

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
def mock_hub(tmp_path, monkeypatch):
    """합성 mock provider 계좌의 (hub, manager, clock)."""
    monkeypatch.setenv("BACH_STATE_MOCK", str(tmp_path / "state.mock.json"))
    cfg = AccountConfig(id="mock", label="모의투자", provider="mock",
                        appkey=None, secret=None, kiwoom_mock=True, danger=False)
    clock = MarketClock(auto=False)
    mgr = FakeManager()
    hub = Hub(cfg, clock, mgr)
    hub._persist = lambda: None  # 테스트 중 state.json 쓰기 방지
    hub.AUTO_SETUP_TIMEOUT = 0.3
    hub.AUTO_SETUP_INTERVAL = 0.01
    yield hub, mgr, clock
    hub.trade_history.close()
    for stock in hub.stocks.values():  # 틱 태스크 정리
        if stock.setup_task:
            stock.setup_task.cancel()
        if stock.task:
            stock.task.cancel()


# 운영 기본값은 총 투자액 100만 원에 하락 시가 허용·트레일링·1차 매도 3호가 지정가가
# 켜져 있다. 전략 단위 테스트는 50만 원·옵션을 끈 기본 경로를 전제로 쓰였으므로 그 값을
# 명시적으로 쓴다.
PLAIN = dict(max_buy_amount=500_000, ulc_allow_lower_open=False, ulc_trailing=False,
             ulc_first_buy_ask3=False)


def plain_config(**overrides) -> AutoConfig:
    return AutoConfig(**{**PLAIN, **overrides})


def make_engine(account=None, logs: list | None = None, *,
                x: float = 10_000.0, z: float = 10_000.0) -> UlcEngine:
    """계좌 응답을 고정한 단독 엔진. account = (qty, avg) 또는 None."""

    async def position_fn():
        return account

    eng = UlcEngine(code="005930", config=plain_config(), x=x, z=z,
                    log=(logs.append if logs is not None else (lambda m: None)),
                    position_fn=position_fn)
    eng.phase = Phase.ACCUMULATING
    return eng
