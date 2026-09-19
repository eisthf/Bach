"""실계좌 경로를 합성 주문으로 검증: 첫 주문 전에 시가와 분할 계획 재검증."""
from unittest.mock import Mock

import pytest

from app.models import Tick, OrderResult, TradeState
from app.strategy.ulc import UlcEngine, Phase


@pytest.mark.parametrize('opened,verified,expected', [
    (34550, True, 333333), (34550, False, None),
    (35000, True, None), (30150, True, 500000),
])
async def test_first_order_uses_verified_open(mock_hub, opened, verified, expected):
    hub, mgr, _ = mock_hub
    stock = hub.add_stock('248170', '샘표식품')
    hub.live = True  # 실제 네트워크 대신 아래 주문 모형만 사용
    stock.machine.state = TradeState.AUTO_TRADING
    stock.config.max_buy_amount = 1000000
    eng = UlcEngine(code=stock.code, config=stock.config, x=30150, z=30150, log=hub._log)
    eng.setup()
    stock.engine = eng
    hub.broker.buy = Mock(return_value=OrderResult(
        ok=True, code=stock.code, side='buy', filled_qty=9, price=opened))
    tick = Tick(code=stock.code, price=opened, open=opened, high=opened,
                low=opened, open_verified=verified)
    await hub._run_engine_tick(stock, tick)
    if expected is None:
        hub.broker.buy.assert_not_called()
    else:
        hub.broker.buy.assert_called_once_with(stock.code, expected)
    if opened == 34550 and verified:
        assert eng.scenario == 3
        assert [leg.target for leg in eng.legs] == [34550, 32800, 31150]
        assert any('시가 보정' in msg for msg in mgr.logs())
    if opened == 35000 and verified:
        assert eng.phase == Phase.SKIPPED


async def test_existing_position_keeps_its_split_plan(mock_hub):
    hub, _, _ = mock_hub
    stock = hub.add_stock('248170', '샘표식품')
    hub.live = True
    stock.machine.state = TradeState.AUTO_TRADING
    eng = UlcEngine(code=stock.code, config=stock.config, x=30150, z=30150, log=hub._log)
    eng.setup()
    eng.legs[0].filled = True
    eng.shares, eng.avg_cost, eng._avg_synced = 14, 34550, True
    stock.engine = eng
    hub.broker.buy = Mock()
    await hub._run_engine_tick(stock, Tick(code=stock.code, price=34000,
        open=34550, high=35000, low=33000, open_verified=True))
    assert eng.z == 30150
    assert eng.scenario == 1
    assert eng.shares == 14
    hub.broker.buy.assert_not_called()
