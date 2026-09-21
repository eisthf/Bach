"""손절 조건 강화(ulc_stop_requires_below_prev_close): 손절선 AND 전일 종가 하회."""
from app.models import AutoConfig, Tick
from app.strategy.ulc import Phase, UlcEngine

X, Z = 10_000, 10_600  # SC2. 1차만(예산 절반 23주) 사면 평단 10,600 → 손절선 10,070 > X


def make(**cfg):
    logs, sells = [], []
    eng = UlcEngine(code="000001", x=X, z=Z, log=logs.append,
                    config=AutoConfig(ulc_first_buy_only=True, **cfg))
    eng.setup()
    return eng, logs, sells


async def feed(eng, sells, *prices):
    async def buy(amount):
        return prices_state["p"]

    async def sell(qty):
        sells.append((qty, prices_state["p"]))
        return prices_state["p"]

    for p in prices:
        prices_state["p"] = p
        await eng.on_tick(Tick(code="000001", price=p, high=p, low=p, open=Z, time=1), buy, sell)


prices_state = {"p": 0}


async def test_default_off_stops_at_stop_line_even_above_prev_close():
    eng, _, sells = make()
    await feed(eng, sells, 10_600, 10_050)
    assert sells == [(23, 10_050)] and eng.phase == Phase.DONE


async def test_stop_requires_both_conditions():
    eng, logs, sells = make(ulc_stop_requires_below_prev_close=True)
    await feed(eng, sells, 10_600, 10_050, 10_000, 10_020)
    # 손절선(10,070) 아래지만 X(10,000) 이상 → 대기. X 와 같은 가격도 대기(X 미만이어야 함).
    assert sells == [] and eng.phase == Phase.HOLDING
    assert sum("손절 대기" in line for line in logs) == 1  # 로그는 한 번만

    await feed(eng, sells, 9_990)
    assert sells == [(23, 9_990)] and eng.phase == Phase.DONE


async def test_manual_handoff_also_requires_both():
    eng, logs, sells = make(ulc_stop_requires_below_prev_close=True, ulc_manual_on_stop=True)
    await feed(eng, sells, 10_600, 10_050)
    assert eng.phase == Phase.HOLDING and not eng.manual_handoff_reason
    await feed(eng, sells, 9_990)
    assert sells == [] and eng.manual_handoff_reason == "손절"


async def test_take_profit_unaffected():
    eng, _, sells = make(ulc_stop_requires_below_prev_close=True)
    await feed(eng, sells, 10_600, 11_130)
    assert sells == [(23, 11_130)]
