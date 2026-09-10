"""장기 실행 인증 복구, 계좌 격리, 주문 재전송 방지."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest
import requests

from app.market_clock import KST
from app.providers import kiwoom_api as kw
from app.providers.kiwoom import KiwoomDataProvider, KiwoomBroker
from app.providers.kiwoom_auth import TokenManager


def grant(token="first", expires="20990101000000"):
    return kw.AccessToken(token, expires)


def response(body, status=200):
    return Mock(status_code=status, json=Mock(return_value=body))


def test_expiry_refresh_and_concurrent_calls_are_merged(monkeypatch):
    import app.providers.kiwoom_auth as auth
    now = datetime(2026, 9, 10, 8, tzinfo=KST).timestamp()
    monkeypatch.setattr(auth.time, "time", lambda: now)
    issue = Mock(side_effect=[grant(expires="20260910080200"), grant("second")])
    monkeypatch.setattr(kw, "fetch_access_token", issue)
    manager = TokenManager("a", "s", True)
    assert manager.get_token() == "first"
    now += 61
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: manager.get_token(), range(8))) == ["second"] * 8
    assert issue.call_count == 2


def test_expiry_uses_korean_timezone(monkeypatch):
    import app.providers.kiwoom_auth as auth
    now = datetime(2026, 9, 10, 7, 59, tzinfo=KST).timestamp()
    monkeypatch.setattr(auth.time, "time", lambda: now)
    monkeypatch.setattr(kw, "fetch_access_token", lambda *a: grant(expires="20260910080100"))
    manager = TokenManager("a", "s", True)
    assert manager.get_token() == "first"
    assert manager._expires_at - now == 120


def test_late_rejection_does_not_invalidate_new_token_or_other_account(monkeypatch):
    monkeypatch.setattr(kw, "fetch_access_token", Mock(side_effect=[grant(), grant("other"), grant("new")]))
    a, b = TokenManager("a", "s", True), TokenManager("b", "s", False)
    a.get_token()
    b.get_token()
    a.reject("first")
    assert a.get_token() == "new"
    a.reject("first")
    assert a.status() == "ok" and b.get_token() == "other"


def test_failed_refresh_is_throttled_and_does_not_expose_credentials(monkeypatch):
    issue = Mock(side_effect=RuntimeError("secret credential text"))
    monkeypatch.setattr(kw, "fetch_access_token", issue)
    manager = TokenManager("a", "s", True)
    for _ in range(3):
        with pytest.raises(kw.KiwoomAuthError) as error:
            manager.get_token()
        assert "secret" not in str(error.value)
    assert issue.call_count == 1
    assert manager.status() == "error"


def test_preemptive_failure_can_use_still_valid_token_but_never_expired_token(monkeypatch):
    import app.providers.kiwoom_auth as auth
    now = datetime(2026, 9, 10, 8, tzinfo=KST).timestamp()
    monkeypatch.setattr(auth.time, "time", lambda: now)
    issue = Mock(side_effect=[grant(expires="20260910080200"), requests.Timeout(), requests.Timeout()])
    monkeypatch.setattr(kw, "fetch_access_token", issue)
    manager = TokenManager("a", "s", True)
    assert manager.get_token() == "first"
    now += 61
    assert manager.get_token() == "first"
    assert manager.get_token() == "first"
    assert issue.call_count == 2
    now += 60
    with pytest.raises(kw.KiwoomAuthError):
        manager.get_token()


def test_non_auth_api_failure_is_not_marked_as_success_or_reissued(monkeypatch):
    issue = Mock(return_value=grant())
    monkeypatch.setattr(kw, "fetch_access_token", issue)
    monkeypatch.setattr(kw, "_reserve_slot", lambda: None)
    monkeypatch.setattr(kw.requests, "post", lambda *a, **k: response({"return_code": 20}))
    provider = KiwoomDataProvider("a", "s", True)
    with pytest.raises(kw.KiwoomRequestError):
        provider.current_upper_limits()
    assert provider.connection_status()["rest"] == "error"
    assert issue.call_count == 1


def test_invalid_token_rest_recovers_once(monkeypatch):
    issue = Mock(side_effect=[grant(), grant("second")])
    post = Mock(side_effect=[
        response({"return_code": 3, "return_msg": "인증 실패[8005:Token이 유효하지 않습니다]"}),
        response({"return_code": 0, "acnt_evlt_remn_indv_tot": [{
            "stk_cd": "A046970", "rmnd_qty": "12", "pur_pric": "1000"}]}),
    ])
    monkeypatch.setattr(kw, "fetch_access_token", issue)
    monkeypatch.setattr(kw.requests, "post", post)
    monkeypatch.setattr(kw, "_reserve_slot", lambda: None)
    broker = KiwoomBroker(KiwoomDataProvider("a", "s", True))
    assert broker.position("046970").quantity == 12
    assert issue.call_count == 2 and post.call_count == 2
    assert post.call_args_list[1].kwargs["headers"]["authorization"] == "Bearer second"


def test_persistent_auth_failure_has_bounded_retry(monkeypatch):
    issue = Mock(side_effect=[grant(), grant("second")])
    monkeypatch.setattr(kw, "fetch_access_token", issue)
    fetch = Mock(side_effect=kw.KiwoomAuthError("invalid"))
    provider = KiwoomDataProvider("a", "s", True)
    for _ in range(2):
        with pytest.raises(kw.KiwoomAuthError):
            provider._call(fetch)
    assert issue.call_count == 2 and fetch.call_count == 2


@pytest.mark.parametrize("failure", ["auth", "timeout", "429"])
def test_order_is_never_replayed(monkeypatch, failure):
    monkeypatch.setattr(kw, "fetch_access_token", lambda *a: grant())
    monkeypatch.setattr(kw, "_reserve_slot", lambda: None)
    monkeypatch.setattr(kw, "_penalize", lambda _: None)
    post = Mock()
    if failure == "timeout":
        post.side_effect = requests.Timeout("unknown result")
    else:
        post.return_value = response({"return_code": 8005}, status=429 if failure == "429" else 200)
    monkeypatch.setattr(kw.requests, "post", post)
    broker = KiwoomBroker(KiwoomDataProvider("a", "s", True))
    assert broker.sell("046970", 1).ok is False
    assert post.call_count == 1


async def test_ws_reauth_uses_new_token_and_requests_position_resync(monkeypatch):
    import websockets
    monkeypatch.setattr(kw, "fetch_access_token", Mock(side_effect=[grant(), grant("second")]))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    class Socket:
        def __init__(self, body):
            self.body = body
            self.send = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            return json.dumps(self.body)

    first = Socket({"trnm": "LOGIN", "return_code": 8005})
    second = Socket({"trnm": "LOGIN", "return_code": 0})
    monkeypatch.setattr(websockets, "connect", Mock(side_effect=[first, second]))
    provider = KiwoomDataProvider("a", "s", True)
    provider.on_order_fill = Mock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await provider._run_ws()
    assert json.loads(first.send.call_args_list[0].args[0])["token"] == "first"
    assert json.loads(second.send.call_args_list[0].args[0])["token"] == "second"
    registration = json.loads(second.send.call_args_list[1].args[0])
    assert registration["trnm"] == "REG"
    assert registration["data"] == [{"item": [], "type": ["00"]}]
    provider.on_order_fill.assert_called_once_with(None)


async def test_renewal_reconnects_existing_socket(monkeypatch):
    provider = KiwoomDataProvider("a", "s", True)
    monkeypatch.setattr(provider._auth, "get_token", lambda: "new")
    provider._ws_token = "old"
    provider._ws = Mock(close=AsyncMock())
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await provider._maintain_auth()
    provider._ws.close.assert_awaited_once()
