"""수동 매매 목록의 자동 손절·익절(평단 대비 % / 가격 라인) 검증."""
import json
from unittest.mock import Mock

import pytest

from app.models import OrderResult, Position, Tick


def tick(price: float) -> Tick:
    return Tick(code="005930", price=price, high=price, low=price, open=10_000.0)


def prepare(mock_hub, stop=None, take_profit=None):
    """평단 10,000원·10주 보유. stop/take_profit = {enabled, mode, pct, price}."""
    hub, manager, clock = mock_hub
    hub.manual_trades.add("005930", "삼성전자")
    hub.broker.position = Mock(return_value=Position(
        code="005930", quantity=10, avg_price=10_000.0, current_price=9_500.0,
    ))
    hub.manual_trades.set_exits("005930", stop, take_profit)
    it = hub.manual_trades.items["005930"]
    clock.open()
    hub.broker.sell = Mock(return_value=OrderResult(
        ok=True, code="005930", side="sell", filled_qty=10, price=9_500.0, order_no="1",
        message="주문 접수"))
    return hub, manager, it


async def run(hub, it, price):
    it.exit_checked_at = 0.0          # 1초 간격 제한을 건너뛴다
    await hub.manual_trades._run_exits(it, tick(price))


async def test_disabled_exits_do_not_query_position(mock_hub):
    hub, _, it = prepare(mock_hub)
    hub.broker.position.reset_mock()
    await run(hub, it, 1.0)
    hub.broker.position.assert_not_called()


async def test_stop_pct_uses_average_price_and_sells_all_once(mock_hub):
    hub, manager, it = prepare(mock_hub, stop={"enabled": True, "mode": "pct", "pct": 5},
                               take_profit={"enabled": True, "mode": "pct", "pct": 10})
    await run(hub, it, 9_501.0)
    assert not hub.broker.sell.called
    await run(hub, it, 9_500.0)                      # 평단 −5% 와 같으면 발동(이하)
    await run(hub, it, 9_400.0)
    hub.broker.sell.assert_called_once_with("005930", 10)
    assert it.exit_triggered
    assert not it.stop.enabled and not it.take_profit.enabled, "주문을 보내면 둘 다 꺼진다"
    assert any("자동 손절: 10주" in text for text in manager.logs())


async def test_stop_line_is_strictly_below(mock_hub):
    hub, _, it = prepare(mock_hub, stop={"enabled": True, "mode": "price", "price": 9_000})
    await run(hub, it, 9_000.0)                      # 라인과 같으면 아직(미만일 때만)
    assert not hub.broker.sell.called
    await run(hub, it, 8_990.0)
    hub.broker.sell.assert_called_once_with("005930", 10)


@pytest.mark.parametrize("rule, below, at", [
    ({"enabled": True, "mode": "pct", "pct": 10}, 10_999.0, 11_000.0),
    ({"enabled": True, "mode": "price", "price": 12_000}, 11_999.0, 12_000.0),
])
async def test_take_profit_at_or_above(mock_hub, rule, below, at):
    hub, manager, it = prepare(mock_hub, take_profit=rule)
    await run(hub, it, below)
    assert not hub.broker.sell.called
    await run(hub, it, at)
    hub.broker.sell.assert_called_once_with("005930", 10)
    assert any("자동 익절: 10주" in text for text in manager.logs())


async def test_pct_line_follows_account_average(mock_hub):
    """비율 방식은 계좌 평단 기준이라 추가 매수로 평단이 바뀌면 발동 가격도 바뀐다."""
    hub, _, it = prepare(mock_hub, stop={"enabled": True, "mode": "pct", "pct": 5})
    hub.broker.position.return_value = Position(code="005930", quantity=20, avg_price=9_000.0)
    await run(hub, it, 9_400.0)                      # 옛 평단 기준이면 발동, 새 평단(8,550)으론 아님
    assert not hub.broker.sell.called
    assert hub.manual_trades.payload("005930")["exits"]["stop"]["level"] == pytest.approx(8_550)


async def test_failed_order_is_retried(mock_hub):
    hub, manager, it = prepare(mock_hub, stop={"enabled": True, "mode": "pct", "pct": 5})
    hub.broker.sell = Mock(return_value=OrderResult(
        ok=False, code="005930", side="sell", filled_qty=0, price=0.0, message="주문 실패"))
    await run(hub, it, 9_500.0)
    assert not it.exit_triggered and it.stop.enabled
    assert any("다음 틱에 재시도" in text for text in manager.logs())
    await run(hub, it, 9_400.0)
    assert hub.broker.sell.call_count == 2


async def test_sell_exception_releases_order_lock(mock_hub):
    hub, manager, it = prepare(mock_hub, stop={"enabled": True, "mode": "pct", "pct": 5})
    hub.broker.sell = Mock(side_effect=RuntimeError("broker unavailable"))
    await run(hub, it, 9_500.0)
    assert not it.exit_triggered and it.stop.enabled
    assert any("주문 오류" in text for text in manager.logs())


async def test_no_orders_outside_session_or_without_holding(mock_hub):
    hub, _, it = prepare(mock_hub, stop={"enabled": True, "mode": "price", "price": 9_000})
    hub.clock.reset()
    await run(hub, it, 8_000.0)
    hub.clock.open()
    hub.broker.position.return_value = Position(code="005930")
    await run(hub, it, 8_000.0)
    assert not hub.broker.sell.called


@pytest.mark.parametrize("stop, take_profit, message", [
    ({"enabled": True, "mode": "price", "price": 12_000}, {"enabled": True, "mode": "price", "price": 11_000},
     "낮아야"),
    ({"enabled": True, "mode": "price", "price": 0}, None, "라인 가격"),
    ({"enabled": True, "mode": "pct", "pct": 100}, None, "비율"),
    ({"enabled": True, "mode": "line"}, None, "방식"),
    # 평단 10,000: 손절 라인 11,000 ≥ 익절(+5% = 10,500)
    ({"enabled": True, "mode": "price", "price": 11_000}, {"enabled": True, "mode": "pct", "pct": 5}, "낮아야"),
])
async def test_invalid_settings_rejected(mock_hub, stop, take_profit, message):
    hub, _, _ = mock_hub
    hub.manual_trades.add("005930")
    hub.broker.position = Mock(return_value=Position(code="005930", quantity=1, avg_price=10_000.0))
    with pytest.raises(ValueError, match=message):
        hub.manual_trades.set_exits("005930", stop, take_profit)


async def test_persist_round_trip_and_legacy(mock_hub, tmp_path):
    hub, _, _ = mock_hub
    m = hub.manual_trades
    m._path = tmp_path / "x.manual.json"
    m.add("005930", "삼성전자")
    m.set_exits("005930", {"enabled": True, "mode": "price", "price": 9_000},
                {"enabled": True, "mode": "pct", "pct": 12})
    saved = json.loads(m._path.read_text(encoding="utf-8"))["items"]
    # 이전 형식(평단 대비 손절만) 항목도 읽는다.
    saved.append({"code": "000660", "name": "SK하이닉스", "stop_enabled": True, "stop_pct": 3})
    m.items.clear()
    m.restore(saved)
    a, b = m.items["005930"], m.items["000660"]
    assert (a.stop.mode, a.stop.price, a.take_profit.pct) == ("price", 9_000, 12)
    assert (b.stop.enabled, b.stop.mode, b.stop.pct, b.take_profit.enabled) == (True, "pct", 3, False)
    for it in m.items.values():
        it.task.cancel()
