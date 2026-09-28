"""시가(Z) 확보 지연 대응: 탈락 사유 진단, 이벤트 기반 셋업 깨우기, VI 대비 제한 시간."""
import asyncio
from datetime import datetime

import pytest

from app import hub as hub_module
from app.hub import Hub
from app.market_clock import KST
from app.models import TradeState
from app.providers import kiwoom as module

VALID = {"10": "31850", "16": "31700", "20": "090020", "290": "2", "9081": "KRX", "15": "1"}


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(module.kw, "fetch_access_token", lambda *a, **k: module.kw.AccessToken("test", "20990101000000"))
    monkeypatch.setattr(module, "now_kst", lambda: datetime(2026, 9, 8, 9, 1, tzinfo=KST))
    return module.KiwoomDataProvider("test", "test", mock=True)


@pytest.fixture
def events(monkeypatch):
    rows = []
    monkeypatch.setattr(module, "record_event", lambda text, **fields: rows.append({"text": text, **fields}))
    return rows


def emit(provider, code="386380", **overrides):
    provider._queues.setdefault(code, asyncio.Queue())
    provider._dispatch_real({"data": [{"type": "0B", "item": code, "values": {**VALID, **overrides}}]})


@pytest.mark.parametrize("field,value,reason", [
    ("290", "1", "session_1"),
    ("9081", "NXT", "exchange_NXT"),
    ("15", "0", "volume_zero"),
    ("16", "0", "open_zero"),
    ("20", "090200", "trade_time_ahead_of_local"),  # PC 시계(09:01:00)가 체결시간보다 늦음
    ("20", "085959", "trade_time_before_open"),
    ("20", "9:0:1", "trade_time_invalid"),
])
def test_rejection_reason_names_the_failed_check(field, value, reason):
    now = datetime(2026, 9, 8, 9, 1, tzinfo=KST)
    values = {**VALID, field: value}
    open_ = module.kw.parse_price(values["16"])
    assert module._open_rejections(values, open_, now) == [reason]
    assert module._open_rejections(VALID, 31700, now) == []


def test_rejected_ticks_are_sampled_then_verified_summary(provider, events):
    for _ in range(7):
        emit(provider, **{"290": "1"})
    emit(provider)
    emit(provider)  # 시가 확보 후 틱은 진단하지 않는다

    rejected = [e for e in events if e["event"] == "open_tick_rejected"]
    verified = [e for e in events if e["event"] == "open_verified"]
    assert len(rejected) == module.OPEN_DIAG_SAMPLES
    assert rejected[0]["reasons"] == ["session_1"]
    assert rejected[0]["fields"]["290"] == "1"
    assert len(verified) == 1
    assert verified[0]["rejected_ticks"] == 7
    assert verified[0]["rejected"] == {"session_1": 7}

    diag = provider.open_diagnostics("386380")
    assert diag["ticks"] == 8 and diag["verified_at"] and diag["first_0b_at"]
    assert provider.day_open("386380") == 31700


def test_diagnostics_silent_outside_open_window(provider, events, monkeypatch):
    monkeypatch.setattr(module, "now_kst", lambda: datetime(2026, 9, 8, 10, 0, tzinfo=KST))
    emit(provider, **{"290": "1", "20": "100000"})
    emit(provider, **{"20": "100000"})
    assert events == []
    assert provider.day_open("386380") == 31700


async def test_wait_day_open_wakes_on_verified_tick(provider, events):
    waiter = asyncio.create_task(provider.wait_day_open("386380", 5.0))
    await asyncio.sleep(0.01)
    emit(provider, **{"9081": "NXT"})  # 탈락 틱으로는 깨지 않는다
    await asyncio.sleep(0.01)
    assert not waiter.done()
    emit(provider)
    await asyncio.wait_for(waiter, 0.5)


async def test_wait_day_open_returns_at_once_when_known_or_times_out(provider, events):
    started = asyncio.get_running_loop().time()
    await provider.wait_day_open("386380", 0.05)
    assert asyncio.get_running_loop().time() - started >= 0.04
    emit(provider)
    await asyncio.wait_for(provider.wait_day_open("386380", 5.0), 0.1)


def test_setup_timeout_covers_static_vi_extension():
    # 정적 VI면 시가 단일가가 2분 연장된다 → 제한 시간은 2분 + 랜덤엔드 여유 이상.
    assert Hub.AUTO_SETUP_TIMEOUT >= 150


def _auto_stock(hub, clock, provider, monkeypatch):
    monkeypatch.setattr(module.kw, "fetch_prev_close", lambda *a, **k: 30150)
    stock = hub.add_stock("386380")
    stock.task.cancel()
    hub.data = provider
    provider._queues[stock.code] = asyncio.Queue()
    clock.open()
    stock.machine.state = TradeState.AUTO_TRADING
    return stock


async def test_setup_wakes_on_open_event_not_poll_interval(provider, events, monkeypatch, mock_hub):
    hub, _, clock = mock_hub
    hub.AUTO_SETUP_TIMEOUT = 60.0
    hub.AUTO_SETUP_INTERVAL = 30.0  # 폴링이었다면 30초 뒤에야 재확인
    stock = _auto_stock(hub, clock, provider, monkeypatch)
    stock.setup_task = asyncio.create_task(hub._prepare_auto(stock))
    await asyncio.sleep(0.05)
    assert stock.engine is None and stock.setup_missing == ("Z",)

    emit(provider, **{"16": "34550", "10": "34600"})
    for _ in range(100):
        if stock.engine is not None:
            break
        await asyncio.sleep(0.01)
    assert stock.engine is not None and stock.engine.z == 34550


async def test_setup_timeout_logs_open_diagnostics(provider, events, monkeypatch, mock_hub):
    hub, _, clock = mock_hub
    logged = []
    monkeypatch.setattr(hub_module, "record_event", lambda text, **fields: logged.append(fields))
    hub.AUTO_SETUP_TIMEOUT = 0.1
    stock = _auto_stock(hub, clock, provider, monkeypatch)
    emit(provider, **{"290": "1"})  # 걸러진 틱만 들어온 경우
    await hub._prepare_auto(stock)

    assert stock.machine.state == TradeState.MANUAL_TRADING
    timeout = next(f for f in logged if f.get("event") == "auto_setup_timeout")
    assert timeout["missing"] == ["Z"]
    assert timeout["open_diag"]["rejected"] == {"session_1": 1}
