"""/ws 초기 스냅샷 전송 중 클라이언트가 끊겨도 구독 큐가 정리되는가.

예전엔 스냅샷 전송이 try 밖이라, 새로고침·탭 닫기로 전송 중 끊기면
WebSocketDisconnect 가 핸들러 밖으로 새어 ASGI 에러 로그가 찍히고
finally(unsubscribe)를 건너뛰어 큐가 _clients 에 영구히 남았다.
"""
from fastapi import WebSocketDisconnect

from app import main


class DroppingSocket:
    """accept 후 n번째 send_json 에서 끊기는 소켓."""

    def __init__(self, fail_at: int):
        self.fail_at = fail_at
        self.sent = 0
        self.query_params = {}

    async def accept(self):
        pass

    async def send_json(self, _msg):
        self.sent += 1
        if self.sent >= self.fail_at:
            raise WebSocketDisconnect(code=1006)


async def test_disconnect_during_snapshot_unsubscribes(monkeypatch):
    monkeypatch.setattr(main, "API_TOKEN", "")
    before = set(main.manager._clients)
    sock = DroppingSocket(fail_at=2)  # 계좌 목록 다음, 장 단계 전송에서 끊김

    await main.ws(sock)  # 예외가 밖으로 새지 않아야 한다

    assert sock.sent == 2
    assert set(main.manager._clients) == before
