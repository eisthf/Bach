from decimal import Decimal

import pytest

from app.pricing import buy_trigger
from conftest import make_engine


@pytest.mark.parametrize("price, expected", [
    (1999.9, 1999), (2004.9, 2000), (4999.9, 4995), (5009.9, 5000),
    (19999.9, 19990), (29022.5, 29000), (50099, 50000),
    (200499, 200000), (500999, 500000),
])
def test_buy_threshold_rounds_down(price, expected):
    assert buy_trigger(price, Decimal(1)) == expected


@pytest.mark.parametrize("z, scenario, targets", [
    (31700, 1, [31700, 29000]),
    (32500, 2, [32500, 30550]),
    (33050, 3, [33050, 31350, 29800]),
])
def test_scenario_targets_use_valid_quotes(z, scenario, targets):
    logs = []
    eng = make_engine(x=30550, z=z, logs=logs)
    eng.setup()
    assert eng.scenario == scenario
    assert [leg.target for leg in eng.legs] == targets
    assert any("1차: 첫 판단 시 시장가" in message for message in logs)


def test_exact_decimal_threshold_is_not_lowered_by_float_error():
    eng = make_engine(x=10000, z=10000)
    eng.config.ulc_q = 0.07
    eng.setup()
    assert eng.legs[1].target == 9300
