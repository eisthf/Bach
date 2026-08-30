"""틱과 봉의 시간축 일치 검증.

봉 시각은 KST 벽시계를 UTC 인 것처럼 환산한 epoch 이다(lightweight-charts 가
UTC 로 렌더링하므로 KST 벽시계를 그대로 보이게 하려는 의도). 예전엔 틱만
진짜 epoch(time.time())이라 두 축이 9시간 어긋났고, 그래서 프런트가 "이 틱이
어느 봉에 속하는가"를 계산할 수 없어 새 봉을 만들지 못했다 — 마지막 봉 하나가
무한히 커지는 표시 왜곡의 원인이었다.
"""
import calendar
from datetime import datetime

from app.market_clock import (
    KST, chart_epoch, now_kst, prev_session_close_epoch, session_open_epoch)
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
    """mock 의 봉과 틱이 같은 축에 놓인다.

    핵심은 틱이 '진짜 epoch'이 아니라 차트 축(chart_epoch)을 쓴다는 것, 그리고
    마지막 봉보다 뒤에 온다는 것이다. 후자가 깨지면(봉이 미래에 놓이면) 프런트의
    롤오버 판정 tick.time >= last.time + step 이 영영 참이 되지 않는다.
    시각 간격 자체는 주말이면 며칠까지 벌어질 수 있으므로 상한을 두지 않는다.
    """
    d = MockDataProvider()
    bars = d.get_bars("005930", 3)
    tick = d.last_tick("005930")
    assert bars, "봉이 비어 있으면 축 비교 불가"
    assert abs(tick.time - chart_epoch()) < 5, "틱이 차트 축을 쓰지 않음"
    assert bars[-1].time <= tick.time, "마지막 봉이 현재보다 미래에 있음"


def test_mock_lookback_bars_sit_on_prev_session():
    """이전 구간 봉은 직전 거래일 마감 이전에 놓인다.

    오늘 09:00 바로 앞에 붙이면 장전(00:00~09:00)에 그 봉들이 '미래'가 되어
    마지막 봉이 현재 틱보다 뒤로 간다. live 의 ka10080 도 전일 봉을 전일
    시각에 준다.
    """
    d = MockDataProvider()
    lookback = 5
    bars = d.get_bars("005930", 3, lookback)
    prev_close = prev_session_close_epoch()
    for b in bars[:lookback]:
        assert b.time < prev_close, "이전 구간 봉이 직전 거래일 마감 이후에 있음"
    assert bars[lookback - 1].time == prev_close - 180, "마지막 이전봉이 마감에 붙어야 함"


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
