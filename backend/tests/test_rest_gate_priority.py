"""REST 게이트: 주문은 대기 중인 봉 조회보다 먼저 나가고, 호출 간격은 유지된다."""
import threading
import time

from app.providers import kiwoom_api as kw

INTERVAL = 0.05


def run_gate(monkeypatch, labels_before, label_after, delay=0.02):
    monkeypatch.setattr(kw, "MIN_REST_INTERVAL", INTERVAL)
    monkeypatch.setattr(kw, "_orders_waiting", 0)
    # 방금 한 건이 나가 다음 슬롯까지 남은 상태.
    monkeypatch.setattr(kw, "_next_allowed", time.monotonic() + 0.1)
    passed = []

    def call(label):
        kw._reserve_slot(priority=label == "order")
        passed.append((label, time.monotonic()))

    threads = [threading.Thread(target=call, args=(lb,)) for lb in labels_before]
    for t in threads:
        t.start()
    time.sleep(delay)  # 봉 조회들이 먼저 줄을 선 뒤에 주문이 도착
    late = threading.Thread(target=call, args=(label_after,))
    late.start()
    for t in [*threads, late]:
        t.join(timeout=5)
    return passed


def test_order_jumps_ahead_of_waiting_bar_requests(monkeypatch):
    passed = run_gate(monkeypatch, ["bars"] * 4, "order")
    assert [lb for lb, _ in passed][0] == "order"
    assert len(passed) == 5


def test_interval_kept_for_all_calls(monkeypatch):
    passed = run_gate(monkeypatch, ["bars"] * 3, "order")
    times = [t for _, t in passed]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(g >= INTERVAL * 0.9 for g in gaps), gaps


def test_normal_calls_without_orders_still_serialized(monkeypatch):
    passed = run_gate(monkeypatch, ["bars"] * 3, "bars")
    assert len(passed) == 4
    times = [t for _, t in passed]
    assert all(b - a >= INTERVAL * 0.9 for a, b in zip(times, times[1:]))
    assert kw._orders_waiting == 0
