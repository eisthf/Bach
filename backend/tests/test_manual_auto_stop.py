"""수동매매 평단 기준 자동 전량 손절 검증."""
from unittest.mock import Mock

from app.models import OrderResult, Position, Tick


def tick(price: float) -> Tick:
    return Tick(code="005930", price=price, high=price, low=price, open=10_000.0)


def prepare(mock_hub, *, enabled: bool = True, pct: float = 5.0):
    hub, manager, clock = mock_hub
    stock = hub.add_stock("005930", "삼성전자")
    stock.config = stock.config.model_copy(update={
        "manual_stop_enabled": enabled,
        "manual_stop_pct": pct,
    })
    clock.open()
    hub.broker.position = Mock(return_value=Position(
        code=stock.code, quantity=10, avg_price=10_000.0, current_price=9_500.0,
    ))
    return hub, manager, stock


async def test_disabled_manual_stop_does_not_query_position(mock_hub):
    hub, _, stock = prepare(mock_hub, enabled=False)

    await hub._run_manual_stop(stock, tick(9_000.0))

    hub.broker.position.assert_not_called()


async def test_manual_stop_uses_average_price_and_sells_all_once(mock_hub):
    hub, manager, stock = prepare(mock_hub)
    hub.broker.sell = Mock(return_value=OrderResult(
        ok=True, code=stock.code, side="sell", filled_qty=10,
        price=9_500.0, order_no="1", message="주문 접수",
    ))

    await hub._run_manual_stop(stock, tick(9_501.0))
    assert not hub.broker.sell.called

    stock.manual_stop_checked_at = 0.0
    await hub._run_manual_stop(stock, tick(9_500.0))
    await hub._run_manual_stop(stock, tick(9_400.0))

    hub.broker.sell.assert_called_once_with(stock.code, 10)
    assert stock.manual_stop_triggered is True
    assert stock.config.manual_stop_enabled is False
    assert any("수동 자동손절: 10주" in text for text in manager.logs())


async def test_manual_stop_failed_order_is_retried(mock_hub):
    hub, manager, stock = prepare(mock_hub)
    hub.broker.sell = Mock(return_value=OrderResult(
        ok=False, code=stock.code, side="sell", filled_qty=0,
        price=0.0, message="주문 실패",
    ))

    await hub._run_manual_stop(stock, tick(9_500.0))

    assert stock.manual_stop_triggered is False
    assert stock.config.manual_stop_enabled is True
    assert any("다음 틱에 재시도" in text for text in manager.logs())

    stock.manual_stop_checked_at = 0.0
    await hub._run_manual_stop(stock, tick(9_400.0))
    assert hub.broker.sell.call_count == 2


async def test_manual_stop_sell_exception_releases_order_lock(mock_hub):
    hub, manager, stock = prepare(mock_hub)
    hub.broker.sell = Mock(side_effect=RuntimeError("broker unavailable"))

    await hub._run_manual_stop(stock, tick(9_500.0))

    assert stock.manual_stop_triggered is False
    assert stock.config.manual_stop_enabled is True
    assert any("주문 오류" in text for text in manager.logs())
