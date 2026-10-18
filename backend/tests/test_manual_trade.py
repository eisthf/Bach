"""수동 매매 목록: 어느 자동매매에서든 언제든 수동 전환, 한 종목 한 방식, 지우기 규칙."""
import asyncio
import json
from unittest.mock import Mock

import pytest

from app.models import Position, TradeState
from conftest import make_engine


async def test_transfer_from_any_state(mock_hub):
    """[매매]의 대기·감시·자동 어느 상태에서든 수동 매매로 옮긴다(되돌리기 없음)."""
    hub, mgr, clock = mock_hub
    for code, state in (("000001", TradeState.IDLE), ("000002", TradeState.MONITOR),
                        ("000003", TradeState.AUTO_TRADING)):
        stock = hub.add_stock(code)
        stock.machine.state = state
        if state == TradeState.AUTO_TRADING:
            stock.engine = make_engine()
        task = stock.task
        await hub.transfer_to_manual(code)
        assert code not in hub.stocks
        assert task.done(), "시세 구독(틱 루프)을 끝낸 뒤 옮긴다"
        assert hub.manual_trades.items[code].origin == "ulc"
        assert {"type": "stock_removed", "code": code, "account": "mock"} in mgr.msgs
    logs = mgr.logs()
    assert any("000003" in t and "AUTO_TRADING → MANUAL" in t for t in logs)
    # 자동매매에서 넘긴 경우만 인계 기록(체결 기록 옆)에 남긴다.
    hub.trade_history.flush()
    events = [e for e in hub.trade_history.events_for() if e["kind"] == "handoff"]
    assert [e["code"] for e in events] == ["000003"]
    with pytest.raises(ValueError):
        hub.add_stock("000003")
    await hub.manual_trades.close()


async def test_transfer_reports_pending_orders(mock_hub):
    hub, _, _ = mock_hub
    hub.add_stock("005930")
    hub.broker.unfilled_orders = Mock(return_value={"005930": [{"order_no": "1"}]})
    item = await hub.transfer_to_manual("005930")
    assert "미체결 주문 1건" in item["notice"]
    await hub.manual_trades.close()


async def test_push_during_auto_moves_to_manual(mock_hub):
    hub, _, clock = mock_hub
    stock = hub.add_stock("005930")
    stock.machine.state = TradeState.AUTO_TRADING
    stock.engine = make_engine()
    clock.open()
    assert await hub.push("005930") == "MANUAL"
    assert "005930" not in hub.stocks and "005930" in hub.manual_trades.items
    await hub.manual_trades.close()


async def test_one_stock_one_method(mock_hub):
    hub, _, _ = mock_hub
    hub.add_stock("000001")
    with pytest.raises(ValueError):
        hub.manual_trades.add("000001")
    hub.manual_trades.add("000002")
    with pytest.raises(ValueError):
        hub.add_stock("000002")
    with pytest.raises(ValueError):
        hub.candle_trades.add_zone("000002")
    await hub.manual_trades.close()


async def test_remove_only_when_flat(mock_hub):
    hub, mgr, _ = mock_hub
    hub.manual_trades.add("005930")
    hub.broker.position = Mock(return_value=Position(code="005930", quantity=3, avg_price=1.0))
    with pytest.raises(ValueError, match="3주"):
        await hub.manual_trades.remove("005930")
    hub.broker.position = Mock(side_effect=RuntimeError("offline"))
    with pytest.raises(ValueError, match="확인하지 못해"):
        await hub.manual_trades.remove("005930")
    hub.broker.position = Mock(return_value=Position(code="005930"))
    await hub.manual_trades.remove("005930")
    assert "005930" not in hub.manual_trades.items
    assert any(m.get("type") == "manual_trade_removed" for m in mgr.msgs)
    hub.add_stock("005930")                      # 지운 뒤에는 다른 방식으로 쓸 수 있다


async def test_fill_routes_to_manual(mock_hub):
    hub, mgr, _ = mock_hub
    hub.manual_trades.add("005930")
    hub._on_order_fill({"code": "005930", "side": "sell", "qty": 2, "price": 1000, "unfilled": 0})
    await asyncio.sleep(0.05)
    assert any("수동매매: 체결: 매도 2주" in t for t in mgr.logs())
    assert any(m.get("type") == "manual_trade" for m in mgr.msgs)
    await hub.manual_trades.close()


async def test_import_held_goes_to_manual(mock_hub):
    hub, _, _ = mock_hub
    hub.add_stock("000001")
    hub.broker.all_positions = Mock(return_value=[
        Position(code="000001", quantity=1), Position(code="000002", quantity=5),
        Position(code="000003", quantity=0)])
    assert await hub.import_held() == ["000002"]
    assert hub.manual_trades.items["000002"].origin == "import"
    await hub.manual_trades.close()


async def test_persist_round_trip(mock_hub, tmp_path):
    hub, _, _ = mock_hub
    hub.manual_trades._path = tmp_path / "x.manual.json"
    hub.manual_trades.add("005930", "삼성전자", "zone", "구간 매매에서 수동 전환")
    hub.manual_trades.set_stop("005930", True, 2.5)
    raw = json.loads(hub.manual_trades._path.read_text(encoding="utf-8"))["items"][0]
    assert (raw["origin"], raw["stop_enabled"], raw["stop_pct"]) == ("zone", True, 2.5)
    with pytest.raises(ValueError):
        hub.manual_trades.set_stop("005930", True, 0)
    await hub.manual_trades.close()
