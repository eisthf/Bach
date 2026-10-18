"""차트용 확인된 체결 기록. 계좌별 상태 파일과 함께 보관한다.

체결 행에는 주문을 낸 쪽이 붙인 전략·사유 태그(``tag_order``)가 함께 남는다 —
이벤트 로그는 넘겨 쓰다 지워지지만 이 기록은 남으므로, 나중에도 어느 전략의 몇 차
매수·어떤 청산이었는지 알 수 있다.
"""
import json
import logging
import queue
import threading
from datetime import datetime, timezone
from uuid import uuid4

from .market_clock import chart_epoch

# 태그로 체결 행에 붙는 필드. strategy: "ulc" | "candle" | "zone" | "manual",
# reason: "1차"·"익절 절반"·"손절" 등 주문 사유, ctx: 전략 맥락(X·Z·기준 캔들·매입 구간 등).
TAG_FIELDS = ("strategy", "reason", "ctx")


def _day(epoch):
    """차트 epoch(KST 벽시계를 UTC로 둔 값) → KST 날짜. 주문번호는 거래일마다 새로 매겨진다."""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y%m%d")


class TradeHistory:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.rows = []
        try:
            self.rows = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(self.rows, list):
                self.rows = []
                raise ValueError("체결 기록은 배열이어야 합니다")
        except FileNotFoundError:
            pass
        except (ValueError, OSError):
            logging.getLogger(__name__).exception("차트 체결 기록 읽기 실패")
        self._ids = {row["id"] for row in self.rows}
        self._by_code = {}
        for row in self.rows:
            self._by_code.setdefault(row["code"], []).append(row)
        self._by_code = {code: tuple(rows) for code, rows in self._by_code.items()}
        # (거래일, 주문번호) → 태그. 체결이 태그보다 먼저 오면 태그가 올 때 그 행에 소급해 붙인다.
        # 프로세스 메모리에만 둔다 — 재시작 전에 낸 주문이 재시작 뒤 체결되면 태그가 없다.
        self._tags = {}
        self._queue = queue.Queue()
        self._stopping = threading.Event()
        self._worker = threading.Thread(target=self._run, name="bach-trade-writer", daemon=True)
        self._worker.start()

    def record(self, fill):
        if fill.get("side") not in ("buy", "sell") or fill.get("qty", 0) <= 0 or fill.get("price", 0) <= 0:
            return
        if self._stopping.is_set():
            return
        # 매매 경로는 수신 시각 캡처와 큐 삽입만 수행한다. 조회/파일 잠금을 잡지 않는다.
        self._queue.put_nowait(dict(fill, time=fill.get("time", chart_epoch())))

    def tag_order(self, order_no, tag, time=None):
        """주문을 낸 쪽이 주문번호에 전략·사유를 붙인다(``TAG_FIELDS``).

        체결 통지(WebSocket)가 주문 응답(REST)보다 먼저 올 수 있어 순서를 가리지 않는다:
        그날 같은 주문번호의 체결 행에 이미 있으면 소급해 붙이고, 나중 체결에도 붙인다.
        매매 경로에서는 큐 삽입만 한다.
        """
        if not order_no or self._stopping.is_set():
            return
        tag = {key: tag[key] for key in TAG_FIELDS if tag.get(key) not in (None, "", {})}
        if tag:
            self._queue.put_nowait(("tag", str(order_no), tag, time or chart_epoch()))

    def _apply_tag(self, order_no, tag, time):
        key = (_day(time), order_no)
        self._tags[key] = tag
        hits = [row for row in self.rows
                if row.get("order_no") == order_no and _day(row["time"]) == key[0]]
        if not hits:
            return
        with self.lock:
            for row in hits:
                row.update(tag)
        self._save()

    def _run(self):
        while True:
            fill = self._queue.get()
            try:
                if fill is None:
                    return
                if isinstance(fill, tuple):
                    self._apply_tag(*fill[1:])
                    continue
                identity = fill.get("identity")
                if identity and identity in self._ids:
                    continue
                row = {key: fill[key] for key in ("code", "side", "qty", "price")}
                row.update(id=identity or uuid4().hex, time=fill["time"],
                           time_source=fill.get("time_source", "received"))
                order_no = str(fill.get("order_no") or "").strip()
                if order_no:
                    row["order_no"] = order_no
                    row.update(self._tags.get((_day(row["time"]), order_no), {}))
                self.rows.append(row)
                self._ids.add(row["id"])
                # 불변 스냅샷을 준비한 뒤 짧게 교체한다. 복사와 I/O는 잠금 밖이다.
                snapshot = self._by_code.get(row["code"], ()) + (row,)
                with self.lock:
                    self._by_code[row["code"]] = snapshot
                self._save()
            except Exception:
                logging.getLogger(__name__).exception("차트 체결 기록 저장 실패")
            finally:
                self._queue.task_done()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.rows, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def flush(self):
        """테스트용 대기. 매매/조회 경로에서는 호출하지 않는다."""
        self._queue.join()

    def close(self):
        """체결 수신을 종료한 후 호출. 대기 중인 기록을 저장하고 종료한다."""
        if not self._stopping.is_set():
            self._stopping.set()
            self._queue.put_nowait(None)
        self._worker.join(timeout=5)
        if self._worker.is_alive():
            logging.getLogger(__name__).warning("차트 체결 기록 종료 대기 초과: 미저장 기록이 남을 수 있습니다")

    def for_code(self, code):
        with self.lock:
            snapshot = self._by_code.get(code, ())
        return [dict(row) for row in snapshot]
