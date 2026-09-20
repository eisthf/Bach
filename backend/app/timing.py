"""주문 경로의 단조 시각 계측. 요청 본문/인증정보는 보관하지 않는다."""
from contextvars import ContextVar
from time import perf_counter_ns

order_trace: ContextVar[dict | None] = ContextVar("order_trace", default=None)


def mark(name: str) -> None:
    trace = order_trace.get()
    if trace is not None:
        trace[name] = perf_counter_ns()


def durations(trace: dict) -> dict:
    pairs = {
        "open_to_setup_ms": ("open_received", "setup_ready"),
        "setup_to_decision_ms": ("setup_ready", "decision"),
        "tick_to_decision_ms": ("tick_received", "decision"),
        "decision_to_worker_ms": ("decision", "worker_start"),
        "worker_to_gate_ms": ("worker_start", "gate_start"),
        "rest_gate_wait_ms": ("gate_start", "http_start"),
        "http_response_ms": ("http_start", "http_end"),
        "decision_to_return_ms": ("decision", "returned"),
        "open_to_http_ms": ("open_received", "http_start"),
        "tick_to_http_ms": ("tick_received", "http_start"),
    }
    return {name: round((trace[end] - trace[start]) / 1_000_000, 3)
            for name, (start, end) in pairs.items()
            if trace.get(start) and trace.get(end) and trace[end] >= trace[start]}
