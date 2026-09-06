"""종목 상태 전환의 장 단계별 가드 검증."""

from app.market_clock import MarketClock
from app.models import TradeState
from app.state_machine import StateMachine


def test_manual_can_schedule_monitor_after_market_close():
    clock = MarketClock()
    clock.close()
    machine = StateMachine(clock)

    assert machine.can_push()
    assert machine.push() == TradeState.MONITOR


def test_manual_can_schedule_monitor_before_market_open():
    clock = MarketClock()
    machine = StateMachine(clock)

    assert machine.can_push()
    assert machine.push() == TradeState.MONITOR


def test_manual_cannot_enter_monitor_while_market_is_open():
    clock = MarketClock()
    clock.open()
    machine = StateMachine(clock)

    assert not machine.can_push()
    assert machine.push() == TradeState.MANUAL_TRADING
