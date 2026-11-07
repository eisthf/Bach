"""[상한가 매매] 2차 이후 분할 매수는 목표가 IOC 지정가.

2026-11-06 한진칼우: SC2 2차(≤ X 62,000) 조건 틱에 시장가를 냈더니 0.5초 새 되튄 65,000~65,100에
체결됐다. 평단은 거의 그대로(65,913)인데 '분할 매수 완료'로 손절이 켜져 2초 뒤 장중 최저가 59,500에
전량 손절. 2차를 목표가 IOC 지정가로 내면 되튄 가격에는 사지 않고, 못 산 동안 손절도 켜지지 않는다.
"""
from app.models import Tick
from app.strategy import ulc
from app.strategy.ulc import BuyLeg, Phase, UlcEngine

from conftest import make_engine

X = 62_000.0


def _tick(price: float) -> Tick:
    return Tick(code="005930", price=price, high=price, low=price, open=65_900.0)


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


def _engine(account=None, logs=None):
    """1차 7주 @ 66,900 체결, 2차(≤ X) 대기인 SC2 엔진."""
    eng = make_engine(account, logs, x=X, z=65_900.0)
    eng.clock = Clock()
    eng.legs = [BuyLeg(target=65_900.0, amount=500_000, filled=True),
                BuyLeg(target=X, amount=500_000)]
    eng.shares, eng.avg_cost, eng._avg_synced = 7, 66_900.0, True
    eng.fill_qty, eng.fill_avg = 7, 66_900.0
    return eng


class Broker:
    """호출측(Hub) 흉내: 지정가·주문번호를 기록하고, 응답 전에 올 체결을 흘려 넣을 수 있다."""

    def __init__(self, eng):
        self.eng = eng
        self.orders = []          # (금액, 지정가)
        self.sells = []
        self.early = []           # 주문 응답 전에 도착할 체결 (수량, 가격)

    async def buy(self, amount):
        no = f"N{len(self.orders) + 1}"
        self.orders.append((amount, self.eng.order_limit))
        for qty, price in self.early:
            self.eng.on_fill("buy", qty, price, no)
        self.early = []
        self.eng.last_order_no = no
        return self.eng.order_limit

    async def sell(self, qty):
        self.sells.append(qty)
        return 59_600.0


async def test_bounced_price_is_not_bought_and_stop_stays_off():
    """조건 틱에 IOC를 냈는데 되튀어 못 샀다 → 1차만 보유, 손절 꺼짐, 손절선 아래여도 안 판다."""
    logs = []
    eng = _engine(logs=logs)
    br = Broker(eng)
    await eng.on_tick(_tick(62_000.0), br.buy, br.sell)
    assert br.orders == [(500_000, X)]                       # 시장가가 아니라 X 지정가
    assert eng.legs[1].order_no == "N1" and not eng.legs[1].filled
    assert eng.shares == 7 and eng.phase == Phase.ACCUMULATING
    assert not eng.all_filled                                 # 손절 비활성

    await eng.on_tick(_tick(59_500.0), br.buy, br.sell)       # 손절선(66,900×0.95) 아래
    assert br.sells == [] and len(br.orders) == 1             # 결과 대기 중엔 재주문도 없다
    assert any("IOC" in m for m in logs)


async def test_miss_is_retried_after_settle_when_price_returns():
    logs = []
    eng = _engine(logs=logs)
    br = Broker(eng)
    await eng.on_tick(_tick(62_000.0), br.buy, br.sell)
    eng.clock.now += ulc.IOC_SETTLE_SEC
    await eng.on_tick(_tick(65_000.0), br.buy, br.sell)       # 정리: 미체결 → 다시 대기
    assert eng.legs[1].order_no == "" and not eng.legs[1].filled
    assert any("미체결" in m for m in logs)
    assert len(br.orders) == 1
    await eng.on_tick(_tick(61_900.0), br.buy, br.sell)       # 다시 X 이하 → 재주문
    assert br.orders == [(500_000, X), (500_000, X)]


async def test_full_fill_completes_leg_and_arms_stop():
    eng = _engine()
    br = Broker(eng)
    await eng.on_tick(_tick(62_000.0), br.buy, br.sell)
    eng.on_fill("buy", 8, 61_900.0, "N1")                     # 8주 = 500,000 // 62,000
    assert eng.legs[1].filled and eng.legs[1].order_no == ""
    assert eng.shares == 15
    assert eng.avg_cost == (7 * 66_900 + 8 * 61_900) / 15    # 실체결 평단
    await eng.on_tick(_tick(62_500.0), br.buy, br.sell)
    assert eng.phase == Phase.HOLDING and eng.all_filled
    await eng.on_tick(_tick(60_000.0), br.buy, br.sell)       # 평단 64,233 × 0.95 = 61,021 아래
    assert br.sells == [15]


async def test_fill_before_order_response_is_attributed_to_leg():
    """체결 통지(WS)가 주문 응답(REST)보다 먼저 와도 그 차수의 체결로 센다."""
    eng = _engine()
    br = Broker(eng)
    br.early = [(8, 62_000.0)]
    await eng.on_tick(_tick(62_000.0), br.buy, br.sell)
    assert eng.legs[1].filled and eng.legs[1].bought == 8
    assert eng.shares == 15


async def test_partial_fill_reorders_remaining_amount():
    logs = []
    eng = _engine(logs=logs)
    br = Broker(eng)
    await eng.on_tick(_tick(62_000.0), br.buy, br.sell)
    eng.on_fill("buy", 3, 62_000.0, "N1")
    assert eng.shares == 10 and not eng.legs[1].filled
    eng.clock.now += ulc.IOC_SETTLE_SEC
    await eng.on_tick(_tick(61_500.0), br.buy, br.sell)
    assert any("일부만 체결 3주" in m for m in logs)
    assert br.orders[1] == (500_000 - 3 * 62_000, X)          # 남은 314,000원으로 재주문
    eng.on_fill("buy", 5, 61_800.0, "N2")                     # 314,000 // 62,000 = 5주
    assert eng.legs[1].filled and eng.legs[1].bought == 8 and eng.shares == 15


async def test_leftover_below_one_share_ends_leg():
    eng = _engine()
    eng.legs[1].amount = 130_000                              # 2주 + 6,000원
    br = Broker(eng)
    await eng.on_tick(_tick(62_000.0), br.buy, br.sell)
    eng.on_fill("buy", 2, 62_000.0, "N1")
    assert eng.legs[1].filled                                 # 주문 수량을 다 샀으니 끝


async def test_other_order_fill_does_not_count_for_leg():
    """다른 주문(예: 늦게 체결된 1차 지정가)의 체결은 2차로 세지 않는다."""
    eng = _engine()
    eng._unconfirmed = 4                                     # 계좌 보정으로 줄여 둔 1차 미체결분
    br = Broker(eng)
    await eng.on_tick(_tick(62_000.0), br.buy, br.sell)
    eng.on_fill("buy", 4, 66_900.0, "FIRST")
    assert eng.legs[1].bought == 0 and not eng.legs[1].filled
    assert eng.shares == 11 and eng._unconfirmed == 0


async def test_rejected_limit_order_skips_leg():
    logs = []
    eng = _engine(logs=logs)

    async def buy(amount):
        return 0.0

    async def sell(qty):
        return 62_000.0   # 차수를 포기하면 예전처럼 손절이 켜진다

    await eng.on_tick(_tick(62_000.0), buy, sell)
    assert eng.legs[1].filled
    assert any("지정가 주문 실패" in m for m in logs)


async def test_caller_without_order_numbers_keeps_immediate_fill():
    """주문번호를 적지 않는 호출측(백테스트)은 예전처럼 즉시 체결로 본다."""
    eng = _engine()

    async def buy(amount):
        assert eng.order_limit == X
        return 61_900.0

    async def sell(qty):
        raise AssertionError

    await eng.on_tick(_tick(61_900.0), buy, sell)
    assert eng.legs[1].filled and eng.shares == 7 + 500_000 // 61_900
    assert eng.order_limit == 0.0


async def test_saved_engine_has_no_pending_ioc():
    eng = _engine()
    br = Broker(eng)
    await eng.on_tick(_tick(62_000.0), br.buy, br.sell)
    eng.on_fill("buy", 3, 62_000.0, "N1")
    back = UlcEngine.from_dict("005930", eng.config, eng.to_dict(), log=lambda m: None)
    assert back.legs[1].order_no == "" and back.legs[1].bought == 3 and not back.legs[1].filled
    view = eng.view()["legs"][1]
    assert view["pending"] and view["bought"] == 3


async def test_hub_sends_ioc_limit_and_mock_fills_reach_engine(mock_hub):
    """Hub: 2차는 broker.buy_limit(IOC). 체결 통지가 없는 mock 은 주문 응답을 곧 체결로 넘긴다."""
    from app.models import TradeState

    hub, _, clock = mock_hub
    stock = hub.add_stock("005930")
    if stock.task:
        stock.task.cancel()
    clock.open()
    stock.machine.state = TradeState.AUTO_TRADING
    eng = _engine()
    eng.position_fn = None
    stock.engine = eng
    price = {"now": 61_000.0}
    hub.broker._price = lambda code: price["now"]
    await hub._run_engine_tick(stock, _tick(61_000.0))
    assert eng.legs[1].filled and eng.legs[1].bought == 500_000 // 62_000
    assert eng.shares == 7 + 8

    # 되튀어 현재가가 지정가보다 높으면 IOC 는 접수만 되고 한 주도 안 산다.
    eng2 = _engine()
    eng2.position_fn = None
    stock.engine = eng2
    price["now"] = 65_000.0
    await hub._run_engine_tick(stock, _tick(62_000.0))
    assert eng2.legs[1].order_no and not eng2.legs[1].filled and eng2.shares == 7
