"""[상한가 매매] 카드의 엔진 요약(UlcEngine.view)과 종목 상태에 실어 보내기."""
import pytest

from app.models import Tick
from app.strategy.ulc import Phase, UlcEngine

from conftest import plain_config

X, Z = 10_000, 10_600  # SC2: X*(1+p)=10,500 ≤ Z < X*(1+p1)=10,700


def make(**cfg):
    eng = UlcEngine(code="000001", x=X, z=Z, log=lambda _: None, config=plain_config(**cfg))
    eng.setup()
    return eng


def test_view_before_buy_shows_scenario_and_plan():
    v = make().view()
    assert (v["scenario"], v["x"], v["z"], v["phase"]) == (2, X, Z, "ACCUMULATING")
    assert [(leg["no"], leg["filled"]) for leg in v["legs"]] == [(1, False), (2, False)]
    assert v["legs"][0]["target"] == Z and v["legs"][1]["target"] == pytest.approx(X)   # SC2 2차 = X
    assert v["avg_cost"] == 0 and v["take_profit"] is None and v["stop"] is None


async def test_view_after_first_buy_has_exit_levels():
    eng = make()

    async def buy(amount):
        return Z

    async def sell(qty):
        return Z

    await eng.on_tick(Tick(code="000001", price=Z, high=Z, low=Z, open=Z, time=1), buy, sell)
    v = eng.view()
    assert v["legs"][0]["filled"] and not v["legs"][1]["filled"] and v["shares"] > 0
    assert v["take_profit"] == pytest.approx(Z * (1 + eng.config.ulc_tp))
    assert v["stop"] == pytest.approx(Z * (1 - eng.config.ulc_sl)) and v["stop_active"] is False


def test_sc3_has_three_legs():
    eng = UlcEngine(code="000001", x=X, z=10_800, log=lambda _: None, config=plain_config())
    eng.setup()
    assert eng.view()["scenario"] == 3 and len(eng.view()["legs"]) == 3


async def test_status_carries_engine_view(mock_hub):
    hub, _mgr, _clock = mock_hub
    stock = hub.add_stock("000001")
    assert hub.status_of("000001").engine is None
    stock.engine = make()
    st = hub.status_of("000001").model_dump()
    assert st["engine"]["scenario"] == 2 and st["engine"]["phase"] == Phase.ACCUMULATING.value
    stock.task.cancel()
