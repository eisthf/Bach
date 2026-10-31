"""차트용 확인된 체결 기록. 계좌별 상태 파일과 함께 보관한다.

체결 행에는 주문을 낸 쪽이 붙인 전략·사유 태그(``tag_order``)가 함께 남는다 —
이벤트 로그는 넘겨 쓰다 지워지지만 이 기록은 남으므로, 나중에도 어느 전략의 몇 차
매수·어떤 청산이었는지 알 수 있다.

서버가 꺼져 있던 동안의 체결(HTS·모바일 주문, 재시작 중 놓친 통지)은 계좌 주문체결내역으로
채운다(``merge_account_orders``) — 그 행은 ``source="account"``, 시각은 주문 시각(``time_source="order"``)이다.

체결이 아닌 매매 이벤트(자동매매 → 사람 인계 등, ``record_event``)는 옆 파일
``state.{계좌}.events.json``에 따로 남긴다 — 체결 기록은 프런트 차트가 그대로 읽는 목록이라 섞지 않는다.
"""
import calendar
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


def _order_epoch(day, hhmmss):
    """YYYYMMDD + 주문 시각 HH:MM:SS → 차트 epoch(KST 벽시계를 UTC로)."""
    try:
        h, m, sec = (int(x) for x in (hhmmss or "00:00:00").split(":"))
    except ValueError:
        h = m = sec = 0
    return calendar.timegm((int(day[:4]), int(day[4:6]), int(day[6:]), h, m, sec))


def events_path_for(path):
    """state.real.trades.json → state.real.events.json (그 밖의 이름은 확장자만 바꿈)."""
    if path.name.endswith(".trades.json"):
        return path.with_name(path.name[: -len(".trades.json")] + ".events.json")
    return path.with_suffix(".events.json")


def _load_list(path, what):
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError(f"{what}은 배열이어야 합니다")
        return rows
    except FileNotFoundError:
        return []
    except (ValueError, OSError):
        logging.getLogger(__name__).exception("%s 읽기 실패", what)
        return []


class TradeHistory:
    def __init__(self, path, events_path=None):
        self.path = path
        self.events_path = events_path or events_path_for(path)
        self.lock = threading.Lock()
        self.rows = _load_list(path, "차트 체결 기록")
        # 매매 이벤트(인계 등). {code, time, kind, strategy, reason, ...}
        self.events = _load_list(self.events_path, "매매 이벤트 기록")
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

    def record_event(self, event):
        """체결이 아닌 매매 이벤트를 남긴다. kind="handoff": 자동매매가 보유 중에 사람에게 넘어감.

        필드: code, kind, strategy(ulc/candle/zone), reason, 그 밖에 qty·ctx 등. time 이 없으면 지금.
        매매 경로에서는 큐 삽입만 한다.
        """
        if not event.get("code") or not event.get("kind") or self._stopping.is_set():
            return
        self._queue.put_nowait(("event", dict(event, time=event.get("time") or chart_epoch())))

    def merge_account_orders(self, day, orders):
        """계좌 주문체결내역(``Broker.order_history``, 주문 단위)으로 그날 빠진 체결을 채운다.

        (거래일, 주문번호)마다 기록된 수량이 계좌 체결 수량보다 모자라면 그만큼 한 행으로 보충한다
        (부분 기록이면 체결 단가에서 기록분을 뺀 평균가). HTS·모바일 주문(channel != "REST API")은
        strategy="manual", reason="수동(경로)". 비교·저장은 기록 스레드에서 체결과 같은 순서로 처리한다.
        """
        if self._stopping.is_set():
            return
        self._queue.put_nowait(("account", str(day), [dict(o) for o in orders or []]))

    def last_day(self):
        """기록된 마지막 체결의 거래일(YYYYMMDD). 기록이 없으면 None."""
        with self.lock:
            return max((_day(row["time"]) for row in self.rows), default=None)

    def _merge_account(self, day, orders):
        mine = [row for row in self.rows
                if row.get("order_no") and _day(row["time"]) == day]
        recorded = {}
        for row in mine:
            q, a = recorded.get(row["order_no"], (0, 0.0))
            recorded[row["order_no"]] = (q + row["qty"], a + row["qty"] * row["price"])
        by_order = {str(o.get("order_no") or "").strip(): o for o in orders}
        changed = set()
        with self.lock:
            # Bach 가 통지는 받았지만 HTS·모바일로 낸 주문(예: 손절 뒤 잔량 수동 매도)도 수동으로 표시한다.
            for row in mine:
                o = by_order.get(row["order_no"])
                channel = (o or {}).get("channel") or ""
                if o and channel and channel != "REST API" and not row.get("strategy"):
                    row.update(strategy="manual", reason=f"수동({channel})", channel=channel)
                    changed.add(row["code"])
        added = []
        for order_no, o in by_order.items():
            code, side = o.get("code"), o.get("side")
            qty, price = int(o.get("qty") or 0), float(o.get("price") or 0)
            if not order_no or not code or side not in ("buy", "sell") or qty <= 0 or price <= 0:
                continue
            q, a = recorded.get(order_no, (0, 0.0))
            missing = qty - q
            if missing <= 0:
                continue
            fill_price = (price * qty - a) / missing if q else price
            if fill_price <= 0:
                fill_price = price
            row = {"code": code, "side": side, "qty": missing, "price": fill_price,
                   "id": f"{day[:4]}-{day[4:6]}-{day[6:]}:{order_no}:account:{q}",
                   "time": _order_epoch(day, o.get("time")), "time_source": "order",
                   "order_no": order_no, "source": "account"}
            channel = o.get("channel") or ""
            if channel:
                row["channel"] = channel
            if channel and channel != "REST API":
                row.update(strategy="manual", reason=f"수동({channel})")
            else:
                row.update(self._tags.get((day, order_no), {}))
            if row["id"] in self._ids:
                continue
            added.append(row)
        if not added and not changed:
            return 0
        for row in added:
            self.rows.append(row)
            self._ids.add(row["id"])
            changed.add(row["code"])
        self._rebuild(changed)
        self._save()
        return len(added)

    def _absorb_account_rows(self, fill_row):
        """계좌 내역으로 보충해 둔 행을 그 주문의 실제 체결 통지가 대신한다(중복 방지).

        보충 직후에 통지가 늦게 도착하면(체결이 진행 중인 주문) 그 수량만큼 보충 행을 줄이고, 0이면 지운다.
        """
        order_no = fill_row["order_no"]
        day = _day(fill_row["time"])
        left = fill_row["qty"]
        supplements = [r for r in self.rows if r.get("source") == "account"
                       and r.get("order_no") == order_no and _day(r["time"]) == day]
        for row in supplements:
            if left <= 0:
                break
            take = min(left, row["qty"])
            left -= take
            with self.lock:
                row["qty"] -= take
            if row["qty"] <= 0:
                self.rows.remove(row)
                self._ids.discard(row["id"])
        return bool(supplements)

    def _rebuild(self, codes):
        snapshots = {code: tuple(row for row in self.rows if row["code"] == code) for code in codes}
        with self.lock:
            self._by_code.update(snapshots)

    def events_for(self, code=None):
        with self.lock:
            return [dict(e) for e in self.events if code is None or e["code"] == code]

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
                if isinstance(fill, tuple) and fill[0] == "account":
                    self._merge_account(*fill[1:])
                    continue
                if isinstance(fill, tuple) and fill[0] == "event":
                    event = {k: v for k, v in fill[1].items() if v not in (None, "", {})}
                    with self.lock:
                        self.events.append(event)
                    self._save_events()
                    continue
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
                    absorbed = self._absorb_account_rows(row)
                else:
                    absorbed = False
                self.rows.append(row)
                self._ids.add(row["id"])
                if absorbed:
                    self._rebuild({row["code"]})
                else:
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

    def _save_events(self):
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.events_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.events, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.events_path)

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

    def summary(self, start_day, end_day):
        """기간(YYYYMMDD, 양 끝 포함)에 체결이 있었던 종목별 요약. 마지막 체결이 최근인 순.

        종목마다 {code, buy_count, sell_count, buy_qty, sell_qty, buy_amount, sell_amount,
        first_day, last_day(YYYY-MM-DD), strategies(체결 행에 붙은 전략 이름들)}.
        """
        with self.lock:
            rows = list(self.rows)
        out = {}
        for row in rows:
            day = _day(row["time"])
            if not start_day <= day <= end_day:
                continue
            s = out.setdefault(row["code"], {
                "code": row["code"], "buy_count": 0, "sell_count": 0, "buy_qty": 0, "sell_qty": 0,
                "buy_amount": 0, "sell_amount": 0, "first_day": day, "last_day": day, "strategies": set(),
            })
            side = row["side"]
            s[f"{side}_count"] += 1
            s[f"{side}_qty"] += int(row["qty"])
            s[f"{side}_amount"] += round(row["qty"] * row["price"])
            s["first_day"], s["last_day"] = min(s["first_day"], day), max(s["last_day"], day)
            if row.get("strategy"):
                s["strategies"].add(row["strategy"])
        result = []
        for s in out.values():
            for key in ("first_day", "last_day"):
                s[key] = f"{s[key][:4]}-{s[key][4:6]}-{s[key][6:]}"
            s["strategies"] = sorted(s["strategies"])
            result.append(s)
        return sorted(result, key=lambda s: (s["last_day"], s["code"]), reverse=True)
