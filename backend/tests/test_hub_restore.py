"""재시작은 자동주문 없이 계좌별로 인계한다 — 보유는 수동 매매로, 나머지 [매매]는 대기로."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.hub import AccountManager, Hub
from app.models import TradeState
from app.providers.kiwoom import KiwoomBroker
from app.providers import kiwoom_api as kw


def saved(hub, tmp_path, entries):
    hub._state_path = tmp_path / f"{hub.account}.json"
    hub._state_path.write_text(json.dumps({"stocks": entries}), encoding="utf-8")
    hub.manual_trades._path = hub._state_path.with_suffix(".manual.json")
    hub._persist = Hub._persist.__get__(hub)


def real_broker(hub, monkeypatch, qty=7):
    # 실제 KiwoomBroker의 캐시/포지션 변환을 사용하되 REST 응답만 대체.
    monkeypatch.setattr(kw, "_post", lambda *a, **k: SimpleNamespace(
        status_code=200, json=lambda: {
            "return_code": 0, "acnt_evlt_remn_indv_tot": [
                {"stk_cd": "A005930", "rmnd_qty": str(qty),
                 "pur_pric": "71000", "cur_prc": "72000"},
            ],
        },
    ))
    hub.broker = KiwoomBroker(SimpleNamespace(
        _call=lambda fn, *a, **k: fn("test", *a, **k),
        _mock=False, last_tick=lambda c: None))
    hub.broker.buy = Mock(side_effect=AssertionError("재시작 주문 금지"))
    hub.broker.sell = Mock(side_effect=AssertionError("재시작 주문 금지"))


@pytest.mark.parametrize("state", [None, "MANUAL_TRADING", "IDLE", "MONITOR", "AUTO_TRADING"])
async def test_restore_holding_moves_to_manual(mock_hub, tmp_path, monkeypatch, state):
    hub, mgr, clock = mock_hub
    clock.open()
    entry = {"code": "005930", "name": "삼성전자",
             "config": {"max_buy_amount": 123456, "manual_stop_enabled": True, "manual_stop_pct": 3}}
    if state:
        entry["state"] = state
    saved(hub, tmp_path, [entry])
    real_broker(hub, monkeypatch)
    await hub.restore()
    assert hub.get("005930") is None
    it = hub.manual_trades.items["005930"]
    assert it.origin == "restore" and it.name == "삼성전자"
    assert "계좌 잔량 7주" in it.notice
    # 예전 [매매] 수동매매의 자동 손절 설정을 이어받는다.
    assert it.stop.enabled and it.stop.mode == "pct" and it.stop.pct == 3
    p = hub.manual_trades.payload("005930")
    assert p["position"]["quantity"] == 7 and p["position"]["avg_price"] == 71000
    assert any(it.notice in log for log in mgr.logs())
    assert json.loads(hub._state_path.read_text(encoding="utf-8"))["stocks"] == []
    saved_manual = json.loads(hub.manual_trades._path.read_text(encoding="utf-8"))["items"]
    assert [e["code"] for e in saved_manual] == ["005930"]
    await hub.apply_market_open()
    hub.broker.buy.assert_not_called()
    hub.broker.sell.assert_not_called()
    await hub.manual_trades.close()


async def test_restore_manual_list_survives_restart(mock_hub, tmp_path, monkeypatch):
    hub, mgr, clock = mock_hub
    saved(hub, tmp_path, [])
    hub.manual_trades.add("000660", "SK하이닉스", "candle", "대금 양봉 매매에서 수동 전환")
    hub.manual_trades.set_exits("000660", {"enabled": True, "mode": "pct", "pct": 4}, None)
    await hub.manual_trades.close()
    hub.manual_trades.items.clear()
    await hub.restore()
    it = hub.manual_trades.items["000660"]
    assert (it.origin, it.stop.enabled, it.stop.pct) == ("candle", True, 4)
    assert "000660" not in hub.stocks
    await hub.manual_trades.close()


async def test_auto_without_position_survives_second_restart(mock_hub, tmp_path):
    hub, mgr, clock = mock_hub
    clock.open()
    saved(hub, tmp_path, [{"code": "005930", "state": "AUTO_TRADING"}])
    await hub.restore()
    notice = hub.status_of("005930").recovery_notice
    assert "AUTO_TRADING" in notice
    await hub.restore()
    assert hub.status_of("005930").recovery_notice == notice
    clock.reset()
    await hub.push("005930")
    assert hub.status_of("005930").recovery_notice == ""
    assert json.loads(hub._state_path.read_text(encoding="utf-8"))["stocks"][0]["state"] == "MONITOR"


async def test_no_saved_file_imports_real_holdings(mock_hub, tmp_path, monkeypatch):
    hub, mgr, clock = mock_hub
    hub._state_path = tmp_path / "missing.json"
    real_broker(hub, monkeypatch)
    await hub.restore()
    assert not hub.stocks
    p = hub.manual_trades.payload("005930")
    assert p["position"]["quantity"] == 7
    assert "어느 목록에도 없던 보유" in p["notice"]
    await hub.manual_trades.close()


async def test_empty_legacy_restore_is_quiet(mock_hub, tmp_path):
    hub, mgr, clock = mock_hub
    saved(hub, tmp_path, [{"code": "005930", "config": {"max_buy_amount": 42}}])
    await hub.restore()
    assert hub.status_of("005930").recovery_notice == ""
    assert hub.status_of("005930").config.max_buy_amount == 42


async def test_account_failure_does_not_block_other_account(mock_hub, tmp_path, monkeypatch):
    hub, mgr, clock = mock_hub
    saved(hub, tmp_path, [{"code": "005930", "state": "AUTO_TRADING"}])
    hub.broker.all_positions = Mock(side_effect=RuntimeError("offline"))
    hub.broker.position = Mock(side_effect=RuntimeError("offline"))
    other = Hub(hub.cfg, clock, mgr)
    other.account = "second"
    saved(other, tmp_path, [{"code": "005930", "config": {"max_buy_amount": 99}}])
    real_broker(other, monkeypatch, qty=3)
    manager = AccountManager.__new__(AccountManager)
    manager.hubs = {"mock": hub, "second": other}
    try:
        await manager.restore()
        assert "잔고 확인 실패" in hub.recovery_notice
        # 잔고를 모르면 옮기지 않고 [매매]에 대기로 둔다 — 보유가 있으면 사람이 [수동 전환].
        assert not hub.status_of("005930").position_verified
        assert "[수동 전환]" in hub.status_of("005930").recovery_notice
        assert other.manual_trades.payload("005930")["position"]["quantity"] == 3
        assert "005930" not in other.stocks
        assert not other.recovery_notice
        assert hub._state_path != other._state_path
    finally:
        await other.manual_trades.close()


@pytest.mark.parametrize("response", [
    None,
    SimpleNamespace(status_code=503),
    SimpleNamespace(status_code=200, json=lambda: {"return_code": 1}),
])
def test_real_balance_errors_are_not_empty_accounts(monkeypatch, response):
    monkeypatch.setattr(kw, "_post", lambda *a, **k: response)
    with pytest.raises(RuntimeError):
        kw.fetch_positions("test", mock=False)


async def test_state_transitions_are_persisted(mock_hub, tmp_path):
    hub, mgr, clock = mock_hub
    saved(hub, tmp_path, [])
    hub.add_stock("005930")
    def state():
        return json.loads(hub._state_path.read_text(encoding="utf-8"))["stocks"][0]["state"]
    await hub.push("005930")
    assert state() == "MONITOR"
    clock.open()
    await hub.apply_market_open()
    await asyncio.gather(*(s.setup_task for s in hub.stocks.values() if s.setup_task))
    assert state() == "AUTO_TRADING"
    assert await hub.push("005930") == "MANUAL"
    assert json.loads(hub._state_path.read_text(encoding="utf-8"))["stocks"] == []
    assert "005930" in hub.manual_trades.items
    await hub.manual_trades.close()


async def test_balance_failure_without_saved_stocks_is_visible(mock_hub, tmp_path):
    hub, mgr, clock = mock_hub
    hub._state_path = tmp_path / "missing.json"
    hub.broker.all_positions = Mock(side_effect=RuntimeError("offline"))
    await hub.restore()
    assert not hub.stocks
    manager = AccountManager.__new__(AccountManager)
    manager.hubs = {hub.account: hub}
    manager.configs = [hub.cfg]
    assert "잔고 확인 실패" in manager.accounts_payload()[0]["recovery_notice"]
    assert any("잔고 확인 실패" in log for log in mgr.logs())


def test_real_balance_invalid_json_raises(monkeypatch):
    response = SimpleNamespace(status_code=200, json=Mock(side_effect=ValueError("json")))
    monkeypatch.setattr(kw, "_post", lambda *a, **k: response)
    with pytest.raises(RuntimeError):
        kw.fetch_positions("test")


async def test_balance_error_at_engine_completion_hands_off(mock_hub):
    from app.models import Tick
    from app.strategy.ulc import Phase
    from conftest import make_engine

    hub, mgr, clock = mock_hub
    clock.open()
    stock = hub.add_stock("005930")
    stock.machine.state = TradeState.AUTO_TRADING
    stock.engine = make_engine()
    stock.engine.phase = Phase.DONE
    hub.broker.position = Mock(side_effect=RuntimeError("offline"))
    await hub._run_engine_tick(stock, Tick(code=stock.code, price=10000,
                                          high=10000, low=10000, open=10000))
    await asyncio.sleep(0.05)  # 수동 매매로 옮기기는 틱 평가 밖에서 돈다
    assert stock.engine is None
    assert stock.code not in hub.stocks
    p = hub.manual_trades.payload(stock.code)
    assert "잔고 확인 실패" in p["notice"]
    assert not p["position_verified"]
    assert not any("청산 완료" in log for log in mgr.logs())
    await hub.manual_trades.close()
