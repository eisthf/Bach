"""차트용 확인된 체결 기록. 계좌별 상태 파일과 함께 보관한다."""
import json
import logging
import queue
import threading
from uuid import uuid4

from .market_clock import chart_epoch


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

    def _run(self):
        while True:
            fill = self._queue.get()
            try:
                if fill is None:
                    return
                identity = fill.get("identity")
                if identity and identity in self._ids:
                    continue
                row = {key: fill[key] for key in ("code", "side", "qty", "price")}
                row.update(id=identity or uuid4().hex, time=fill["time"],
                           time_source=fill.get("time_source", "received"))
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
