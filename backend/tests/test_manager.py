"""AccountManager WS 버스 검증 — 느린 클라이언트가 좀비를 만들지 않는가.

예전 broadcast 는 큐가 가득 찬 클라이언트를 _clients 에서 퇴출했다. 그러면
그 큐를 기다리는 /ws 핸들러는 put 이 끊겨 q.get() 에서 영원히 잠들고 소켓이
좀비로 남았다. 지금은 가장 오래된 메시지를 버리고 최신을 넣는다 — 메시지가
계속 흐르므로 죽은 소켓은 다음 send 실패로 핸들러가 깨어나 정리된다.
"""
import pytest

from app.hub import AccountManager


@pytest.fixture
def manager(monkeypatch):
    """합성 mock 계좌 1개짜리 AccountManager (네트워크·자격증명 무의존)."""
    monkeypatch.setenv("ACCOUNTS", "mock")
    monkeypatch.setenv("MOCK_PROVIDER", "mock")
    return AccountManager()


def test_slow_client_not_evicted_drops_oldest(manager):
    q = manager.subscribe()
    cap = q.maxsize
    for i in range(cap):  # 큐를 가득 채운다(느린 클라이언트 모사)
        q.put_nowait({"i": i})

    manager.broadcast({"type": "tick", "last": True})

    # 퇴출되지 않는다 — 핸들러가 q.get() 에서 영원히 잠드는 일이 없다.
    assert q in manager._clients
    # 가장 오래된 것({"i": 0})이 버려지고 최신이 꼬리에 들어간다.
    assert q.qsize() == cap
    drained = [q.get_nowait() for _ in range(cap)]
    assert drained[0] == {"i": 1}
    assert drained[-1] == {"type": "tick", "last": True}


def test_normal_client_receives_in_order(manager):
    q = manager.subscribe()
    for i in range(3):
        manager.broadcast({"i": i})
    assert [q.get_nowait() for _ in range(3)] == [{"i": 0}, {"i": 1}, {"i": 2}]
    manager.unsubscribe(q)
    manager.broadcast({"i": 99})
    assert q.empty(), "구독 해지 후에는 수신하지 않는다"
