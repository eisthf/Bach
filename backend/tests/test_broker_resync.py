"""재접속 후 잔고 경고·초기 시세 복구와 계좌별 상태 전달."""
from unittest.mock import Mock

from app.models import Position, Tick


async def test_reconnect_refreshes_balance_name_and_initial_quote(mock_hub):
    hub, manager, _ = mock_hub
    stock = hub.add_stock("046970")
    hub.live = True
    hub.data.stock_name = Mock(return_value="우리로")
    hub.data.last_tick = Mock(return_value=Tick(code="046970", price=1295, high=1295, low=1000, open=1000))
    hub.broker.all_positions = Mock(return_value=[Position(code="046970", quantity=7)])
    hub.broker.position = Mock(return_value=Position(code="046970", quantity=7))
    await hub._refresh_all_positions()
    assert stock.name == "우리로" and hub._sync_state == "ok"
    statuses = [m["status"] for m in manager.msgs if m["type"] == "status"]
    assert statuses[-1]["position_verified"] is True
    assert statuses[-1]["position"]["quantity"] == 7
    assert any(m["type"] == "tick" and m["tick"]["price"] == 1295 for m in manager.msgs)
    assert all(m["account"] == hub.account for m in manager.msgs)


async def test_reconnect_balance_failure_stays_unverified(mock_hub):
    hub, manager, _ = mock_hub
    hub.add_stock("046970")
    hub.broker.all_positions = Mock(side_effect=RuntimeError("offline"))
    hub.broker.position = Mock(side_effect=RuntimeError("offline"))
    await hub._refresh_all_positions()
    assert hub._sync_state == "error"
    statuses = [m["status"] for m in manager.msgs if m["type"] == "status"]
    assert statuses[-1]["position_verified"] is False
