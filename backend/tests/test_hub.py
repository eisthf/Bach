"""Hub 통합 검증 — 틱 경로, 체결 라우팅, 청산 잔량, 장 시작 셋업.

mock provider 로 실제 Hub 를 조립해 상태머신·엔진·브로드캐스트가 함께
도는 것을 확인한다.
"""
import asyncio
import time

from app.models import Position, Tick, TradeState
from app.strategy.ulc import Phase


def _slow_orders(hub, sec: float) -> None:
    """mock 브로커는 즉시 반환이라, 실주문(HTTP+rate-limit 대기)처럼 늦춘다."""
    for name in ("buy", "sell"):
        orig = getattr(hub.broker, name)

        def slow(*a, _f=orig, **kw):
            time.sleep(sec)
            return _f(*a, **kw)

        setattr(hub.broker, name, slow)


def _to_auto(hub, clock, code: str, name: str = ""):
    """종목 추가 → MONITOR → 장 시작. 자체 틱루프는 꺼서 수동 틱 주입."""
    stock = hub.add_stock(code, name)
    if stock.task:
        stock.task.cancel()
    stock.machine.state = TradeState.MONITOR
    clock.open()
    return stock


async def test_tick_path_nonblocking_and_transitions(mock_hub):
    """주문 2건×0.3초 동안 루프 생존 + 매수→청산→POSITION-FLAT 전이."""
    hub, mgr, clock = mock_hub
    _slow_orders(hub, 0.3)
    stock = _to_auto(hub, clock, "005930", "삼성전자")
    await hub.apply_market_open()
    await asyncio.gather(*(s.setup_task for s in hub.stocks.values() if s.setup_task))
    assert stock.machine.state == TradeState.AUTO_TRADING
    eng = stock.engine
    assert eng is not None and eng.phase == Phase.ACCUMULATING

    beats = 0

    async def hb():
        nonlocal beats
        while True:
            beats += 1
            await asyncio.sleep(0.01)

    t = asyncio.create_task(hb())
    await asyncio.sleep(0.05)
    base, before = beats, len(mgr.msgs)

    # 1차 매수(즉시 발화) → 익절가 도달 → 전량 청산.
    # 가격은 명시적으로 고정한다. mock 랜덤워크 결과에 맡기면 진입 가드
    # (현재가 >= X*(1+w) → SKIP)나 매수 직후 즉시 익절이 우발적으로 걸려
    # 비결정적이 된다. z 는 setup 필터를 통과한 값이라 가드에 안 걸리고,
    # 브로커 체결가(last_tick)도 같은 값으로 맞춰 평단==틱가 → 즉시 익절 없음.
    price = eng.z
    hub.data._last_tick["005930"] = Tick(code="005930", price=price, high=price,
                                         low=price, open=price, time=0)
    await hub._run_engine_tick(stock, Tick(code="005930", price=price, high=price,
                                           low=price, open=price))
    assert eng.shares > 0, "1차 매수 미체결"

    tp = eng.avg_cost * (1 + stock.config.ulc_tp) * 1.01
    for leg in eng.legs:  # 분할 완료 처리(손절/익절 활성화)
        leg.filled = True
    eng.phase = Phase.HOLDING
    await hub._run_engine_tick(stock, Tick(code="005930", price=tp, high=tp,
                                           low=tp, open=eng.z))
    during = beats - base
    t.cancel()

    assert eng.phase == Phase.DONE and eng.shares == 0
    assert stock.machine.state == TradeState.MANUAL_TRADING
    assert stock.engine is None, "엔진이 정리되지 않음"
    assert during >= 20, f"루프가 막혔다 (heartbeat {during}회, 기대 ~60)"
    assert all(m.get("account") == "mock" for m in mgr.msgs), "계좌 태그 누락"
    assert any(m.get("type") == "status" for m in mgr.msgs[before:])


async def test_market_open_setup_nonblocking(mock_hub):
    """09:00 장 시작: 종목당 0.3초 REST × 3종목 셋업 중 루프 생존 (5b3e7ba).

    지연 대상은 prev_close — 정상 경로에서 실제 호출되는 REST 다.
    (get_bars 는 X/Z 폴백에서만 호출되므로 여기 지연을 걸면 무의미.)
    """
    hub, mgr, clock = mock_hub
    orig = hub.data.prev_close

    def slow_prev_close(*a, **kw):
        time.sleep(0.3)
        return orig(*a, **kw)

    hub.data.prev_close = slow_prev_close
    hub.AUTO_SETUP_TIMEOUT = 2.0
    for code in ("005930", "000660", "035720"):
        _to_auto(hub, clock, code)

    beats = 0

    async def hb():
        nonlocal beats
        while True:
            beats += 1
            await asyncio.sleep(0.01)

    t = asyncio.create_task(hb())
    await asyncio.sleep(0.05)
    base = beats
    await hub.apply_market_open()
    await asyncio.gather(*(s.setup_task for s in hub.stocks.values() if s.setup_task))
    during = beats - base
    t.cancel()

    assert sum(1 for s in hub.stocks.values() if s.engine) == 3
    assert during >= 30, f"장 시작 셋업이 루프를 막음 (heartbeat {during}회)"


async def test_fill_event_routed_to_engine(mock_hub):
    """00 체결 이벤트가 자동매매 중인 엔진의 평단을 확정한다 (95fd355)."""
    hub, mgr, clock = mock_hub
    stock = _to_auto(hub, clock, "005930", "삼성전자")
    await hub.apply_market_open()
    await asyncio.gather(*(s.setup_task for s in hub.stocks.values() if s.setup_task))

    hub._on_order_fill({"code": "005930", "side": "buy", "qty": 10,
                        "price": 12_345.0, "unfilled": 0, "order_no": "1"})
    await asyncio.sleep(0)  # create_task 정리
    assert stock.engine.avg_cost == 12_345.0
    assert stock.engine.fill_qty == 10

    # 엔진 없는 종목의 체결은 조용히 무시(크래시 없음)
    stock.engine = None
    hub._on_order_fill({"code": "005930", "side": "sell", "qty": 5,
                        "price": 1.0, "unfilled": 0, "order_no": "2"})
    await asyncio.sleep(0)


async def _run_residual_scenario(mock_hub, residual_qty: int):
    """엔진은 DONE(청산 완료 믿음), 계좌는 residual_qty 를 보고하는 상황."""
    hub, mgr, clock = mock_hub
    stock = _to_auto(hub, clock, "005930", "삼성전자")
    await hub.apply_market_open()
    await asyncio.gather(*(s.setup_task for s in hub.stocks.values() if s.setup_task))
    eng = stock.engine
    eng.phase = Phase.DONE
    eng.shares = 0
    hub.broker.position = lambda c, _q=residual_qty: Position(
        code=c, quantity=_q, avg_price=10_000.0, current_price=10_000.0)

    before = len(mgr.msgs)
    await hub._run_engine_tick(stock, Tick(code="005930", price=10_000.0,
                                           high=10_000.0, low=10_000.0,
                                           open=10_000.0))
    return stock, mgr.logs(before)


async def test_clean_liquidation_logged(mock_hub):
    """계좌 잔량 0 — '청산 완료' 기록, 경고 없음 (5191d4d)."""
    stock, logs = await _run_residual_scenario(mock_hub, 0)
    assert any("청산 완료" in m for m in logs)
    assert not any("⚠️" in m for m in logs)
    assert stock.machine.state == TradeState.MANUAL_TRADING
    assert stock.engine is None


async def test_residual_position_warned_not_falsely_done(mock_hub):
    """계좌 잔량 7주 — '청산 완료' 오기록 없이 경고 + 수동 인계 (5191d4d).

    예전에는 엔진이 DONE 이면 무조건 '청산 완료(보유수량 0)'로 기록해,
    잔량이 남아도 아무도 관리하지 않는 포지션이 조용히 생겼다.
    """
    stock, logs = await _run_residual_scenario(mock_hub, 7)
    assert any("⚠️" in m and "7주" in m for m in logs)
    assert not any("청산 완료" in m for m in logs)
    assert stock.machine.state == TradeState.MANUAL_TRADING
    assert stock.engine is None, "중복매도 방지 위해 엔진은 정리돼야 함"
