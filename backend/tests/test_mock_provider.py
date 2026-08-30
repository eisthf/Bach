"""mock 시뮬레이터의 재현성·표시 정합성 검증."""
import os
import pathlib
import subprocess
import sys

import app
from app.providers.mock import MockDataProvider, _seed_for

BACKEND = str(pathlib.Path(app.__file__).resolve().parent.parent)


def test_seed_survives_hash_randomization():
    """시드가 PYTHONHASHSEED 에 영향받지 않아야 한다.

    예전엔 str.hash 를 써서 해시 랜덤화에 걸렸고, 재시작할 때마다 같은 종목의
    차트·시나리오가 통째로 바뀌어 "어제 본 그 상황 재현"이 불가능했다.
    해시 시드를 바꿔가며 별도 프로세스로 돌려 결정성을 확인한다.
    """
    snippet = (
        f"import sys; sys.path.insert(0, {BACKEND!r}); "
        "from app.providers.mock import _seed_for; print(_seed_for('005930'))"
    )
    outs = set()
    for hashseed in ("0", "1", "12345"):
        r = subprocess.run([sys.executable, "-c", snippet], capture_output=True,
                           text=True, env={**os.environ, "PYTHONHASHSEED": hashseed})
        assert r.returncode == 0, r.stderr
        outs.add(r.stdout.strip())
    assert outs == {str(_seed_for("005930"))}, f"해시 시드에 따라 달라짐: {outs}"


def test_same_code_same_scenario():
    """같은 종목코드는 같은 X/Z 를 낳는다(인스턴스가 달라도)."""
    a, b = MockDataProvider(), MockDataProvider()
    a._ensure_base("005930")
    b._ensure_base("005930")
    assert a._prev_close["005930"] == b._prev_close["005930"]
    assert a._open_price["005930"] == b._open_price["005930"]


def test_different_codes_differ():
    d = MockDataProvider()
    d._ensure_base("005930")
    d._ensure_base("000660")
    assert d._prev_close["005930"] != d._prev_close["000660"]


def test_session_high_low_matches_day_bars():
    """헤더의 고가/저가(tick)가 차트의 당일 범위와 일치해야 한다.

    예전엔 마지막 봉 종가로 초기화해, 실제로는 '앱을 켠 뒤의' 고저였고
    차트가 보여주는 당일 범위와 어긋났다(live 의 FID 17/18 은 진짜 당일 고저).
    """
    d = MockDataProvider()
    lookback = 60
    bars = d.get_bars("005930", 3, lookback)
    day = bars[lookback:] or bars
    tick = d.last_tick("005930")
    assert tick.high == max(b.high for b in day)
    assert tick.low == min(b.low for b in day)
    assert tick.low <= tick.price <= tick.high


def test_tick_open_preserves_day_open():
    """tick.open 은 당일 시가 Z 를 유지한다(ULC 시나리오 판정에 쓰임)."""
    d = MockDataProvider()
    d.get_bars("005930", 3)
    assert d.last_tick("005930").open == d.day_open("005930")
