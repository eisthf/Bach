"""백테스트: 1분봉 재생이 운영 엔진 판단을 그대로 재현하는가 (네트워크 무의존)."""
import json

import pytest

from app.backtest import DayData, bar_prices, day_from_rows, load_day, minute_epoch, parse_case, replay
from app.models import AutoConfig

DATE = "20260903"


def bar(hhmm, o, h, l, c):
    return {"_hhmm": hhmm, "time": minute_epoch(DATE, hhmm), "open": o, "high": h, "low": l, "close": c, "volume": 1}


def test_bar_prices_order():
    up, down = bar("0900", 100, 120, 90, 110), bar("0901", 110, 120, 90, 100)
    assert bar_prices(up) == [100, 90, 120, 110]      # 양봉: 저가 먼저
    assert bar_prices(down) == [110, 120, 90, 100]    # 음봉: 고가 먼저
    assert bar_prices(up, "high_first") == [100, 120, 90, 110]
    with pytest.raises(ValueError):
        bar_prices(up, "random")


async def test_sc2_split_buy_then_stop_loss():
    # X=6790, Z=7190(+5.9% → SC2). 2차 6,790 체결 후 평단 -5% 하회 → 손절.
    day = DayData("079650", DATE, x=6790, z=7190, bars=[
        bar("0900", 7190, 7190, 7190, 7190),
        bar("0903", 7030, 7030, 6790, 6790),
        bar("0908", 6700, 6700, 6620, 6620),
        bar("0937", 8600, 8600, 8600, 8600),
    ])
    r = await replay(day, AutoConfig(max_buy_amount=1_000_000))
    assert [(t.side, t.qty, t.price) for t in r.trades] == [
        ("buy", 69, 7190), ("buy", 73, 6790), ("sell", 142, 6620)]
    assert r.held == 0
    assert r.pnl == 142 * 6620 - (69 * 7190 + 73 * 6790)
    assert r.net_pnl < r.pnl


async def test_first_buy_ask3_fills_two_ticks_above():
    day = DayData("079650", DATE, x=6790, z=7190, bars=[bar("0900", 7190, 7190, 7190, 7190)])
    r = await replay(day, AutoConfig(ulc_first_buy_ask3=True))
    assert r.trades[0].price == 7210  # 7,190 + 2호가(10원)
    assert r.held == r.trades[0].qty and r.mark == r.held * 7190  # 미청산은 종가 평가


async def test_gap_filter_skips_without_trades():
    day = DayData("079650", DATE, x=6790, z=7900, bars=[bar("0900", 7900, 7900, 7900, 7900)])
    r = await replay(day, AutoConfig())
    assert r.trades == [] and any("SKIP" in line for line in r.logs)


async def test_bars_after_regular_close_are_ignored():
    day = DayData("079650", DATE, x=6790, z=6800, bars=[
        bar("0900", 6800, 6800, 6800, 6800), bar("1600", 5000, 5000, 5000, 5000)])
    r = await replay(day, AutoConfig(ulc_first_buy_only=True))
    assert [t.side for t in r.trades] == ["buy"]


def test_day_from_rows_uses_previous_close_and_day_open():
    daily = [{"_date": "20260902", "open": 5360, "close": 6790},
             {"_date": DATE, "open": 7190, "close": 7570}]
    day = day_from_rows("079650", DATE, daily, [bar("0900", 1, 1, 1, 1)])
    assert (day.x, day.z) == (6790, 7190)
    with pytest.raises(ValueError):
        day_from_rows("079650", "20260904", daily, [bar("0900", 1, 1, 1, 1)])


def test_load_day_uses_cache_without_token(tmp_path):
    raw = {"daily": [{"_date": "20260902", "open": 1, "close": 6790},
                     {"_date": DATE, "open": 7190, "close": 1}],
           "minutes": [bar("0900", 7190, 7190, 7190, 7190)]}
    (tmp_path / f"079650_{DATE}.json").write_text(json.dumps(raw), encoding="utf-8")

    def no_token():
        raise AssertionError("캐시 적중 시 토큰을 발급하면 안 된다")
    assert load_day("079650", DATE, token_fn=no_token, mock=False, cache_dir=tmp_path).x == 6790


def test_parse_case():
    assert parse_case("079650:2026-09-03") == ("079650", DATE)
    with pytest.raises(ValueError):
        parse_case("79650:20260903")
