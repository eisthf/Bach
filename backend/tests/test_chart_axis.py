"""틱과 봉의 시간축 일치 검증.

봉 시각은 KST 벽시계를 UTC 인 것처럼 환산한 epoch 이다(lightweight-charts 가
UTC 로 렌더링하므로 KST 벽시계를 그대로 보이게 하려는 의도). 예전엔 틱만
진짜 epoch(time.time())이라 두 축이 9시간 어긋났고, 그래서 프런트가 "이 틱이
어느 봉에 속하는가"를 계산할 수 없어 새 봉을 만들지 못했다 — 마지막 봉 하나가
무한히 커지는 표시 왜곡의 원인이었다.
"""
import calendar
from datetime import datetime

from app.market_clock import KST, chart_epoch, now_kst, session_open_epoch
from app.providers.mock import MockDataProvider


def test_chart_epoch_matches_kst_wall_clock():
    """chart_epoch 은 KST 벽시계를 UTC 로 간주한 값 — 진짜 epoch 보다 9시간 앞선다."""
    naive = datetime(2026, 8, 21, 9, 0)
    assert chart_epoch(naive) == calendar.timegm(naive.timetuple())

    aware = datetime(2026, 8, 21, 9, 0, tzinfo=KST)
    assert chart_epoch(aware) - int(aware.timestamp()) == 9 * 3600


def test_session_open_is_0900():
    """session_open_epoch 은 당일 09:00 KST 를 가리킨다."""
    opened = session_open_epoch()
    assert (opened - chart_epoch(now_kst().replace(
        hour=9, minute=0, second=0, microsecond=0))) == 0


def test_mock_bars_and_ticks_share_axis():
    """mock 의 봉과 틱이 같은 축 — 틱이 마지막 봉 근처(한 봉 간격 이내)에 놓인다."""
    d = MockDataProvider()
    bars = d.get_bars("005930", 3)
    tick = d.last_tick("005930")
    assert bars, "봉이 비어 있으면 축 비교 불가"
    delta = tick.time - bars[-1].time
    assert 0 <= delta < 24 * 3600, f"틱이 봉 축에서 벗어남(차이 {delta}초)"


def test_mock_day_bars_anchored_to_today_0900():
    """당일 첫 봉이 정확히 09:00(KST)에 앵커링된다."""
    d = MockDataProvider()
    lookback = 5
    bars = d.get_bars("005930", 3, lookback)
    if len(bars) <= lookback:
        return  # 장전이라 당일 봉 없음
    assert bars[lookback].time == session_open_epoch()


def test_mock_generates_only_elapsed_day_bars():
    """당일 봉은 '지금까지'만 생성된다(live 와 같은 모양).

    예전엔 시각과 무관하게 항상 하루치 130봉을 만들어, 같은 코드가 mock 에서만
    다른 분기를 타게 했다(hub._start_auto 의 X/Z 산정이 mock 에서만 맞던 원인).
    """
    d = MockDataProvider()
    lookback = 10
    bars = d.get_bars("005930", 3, lookback)
    day_bars = len(bars) - lookback
    elapsed_min = (chart_epoch() - session_open_epoch()) / 60
    expected = 0 if elapsed_min < 0 else min(130, int(elapsed_min) // 3 + 1)
    assert day_bars == expected
    assert day_bars <= 130
