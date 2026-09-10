"""분봉 조회 단기 캐시 검증 (KiwoomDataProvider.get_bars).

같은 (종목,간격)을 여러 곳이 거의 동시에 요청한다 — 차트 카드 여러 개 마운트,
브라우저 탭 여러 개, 봉 경계에서 전 차트 동시 재조회. ka10080 은 페이징까지
하는데 REST 게이트가 0.35초 간격으로 직렬화하므로 중복이 지연으로 쌓인다.
"""
import threading
import time

import pytest

from app.models import Bar
from app.providers.kiwoom_api import AccessToken
from app.providers.base import DAY_INTERVAL
from app.providers.kiwoom import KiwoomDataProvider


@pytest.fixture
def provider(monkeypatch):
    """네트워크 없이 KiwoomDataProvider 를 만든다(토큰 발급·조회 스텁)."""
    monkeypatch.setattr("app.providers.kiwoom.kw.fetch_access_token",
                        lambda *a, **kw: AccessToken("test-token", "20990101000000"))
    p = KiwoomDataProvider(appkey="k", secretkey="s", mock=True)
    p.calls = 0

    def fake_fetch(code, interval, lookback_extra):
        p.calls += 1
        time.sleep(0.05)  # 네트워크 지연 흉내(동시요청 병합 관찰용)
        return [Bar(time=1_788_000_000, open=1, high=2, low=1, close=2, volume=1)]

    p._fetch_bars = fake_fetch
    return p


def test_repeated_calls_hit_cache(provider):
    for _ in range(5):
        provider.get_bars("005930", 3)
    assert provider.calls == 1, "TTL 안에서는 한 번만 조회해야 함"


def test_concurrent_calls_collapse(provider):
    """동시 요청은 하나로 합쳐진다(키별 락)."""
    def worker():
        provider.get_bars("005930", 3)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert provider.calls == 1, f"동시 요청이 합쳐지지 않음(조회 {provider.calls}회)"


def test_different_keys_not_shared(provider):
    provider.get_bars("005930", 3)
    provider.get_bars("005930", 5)       # 다른 간격
    provider.get_bars("000660", 3)       # 다른 종목
    provider.get_bars("005930", 3, 10)   # 다른 lookback
    assert provider.calls == 4


def test_bar_boundary_invalidates(provider, monkeypatch):
    """봉 경계를 넘으면 캐시가 자동 무효화된다 — 새 봉이 안 보이면 안 되므로."""
    base = 1_788_000_000
    monkeypatch.setattr("app.providers.kiwoom.chart_epoch", lambda: base)
    provider.get_bars("005930", 3)
    provider.get_bars("005930", 3)
    assert provider.calls == 1

    # 3분 경계를 넘김 → 버킷이 달라져 재조회
    monkeypatch.setattr("app.providers.kiwoom.chart_epoch", lambda: base + 180)
    provider.get_bars("005930", 3)
    assert provider.calls == 2


def test_ttl_expiry_refetches(provider, monkeypatch):
    """같은 버킷 안이라도 TTL 이 지나면 다시 받는다(진행 중 봉 갱신)."""
    monkeypatch.setattr(KiwoomDataProvider, "_BARS_TTL", 0.05)
    provider.get_bars("005930", 3)
    assert provider.calls == 1
    time.sleep(0.08)
    provider.get_bars("005930", 3)
    assert provider.calls == 2


def test_returned_list_is_isolated(provider):
    """호출자가 반환 리스트를 건드려도 캐시가 오염되지 않는다."""
    a = provider.get_bars("005930", 3)
    a.append(Bar(time=1, open=1, high=1, low=1, close=1, volume=0))
    b = provider.get_bars("005930", 3)
    assert len(b) == 1


def test_current_daily_bars_are_cached_until_next_day(provider, monkeypatch):
    """오늘 일봉을 받은 뒤에는 3초 TTL이 지나도 과거 120개를 다시 받지 않는다."""
    day = (1_788_000_000 // 86_400) * 86_400
    clock = {"now": day + 9 * 3600}
    monkeypatch.setattr("app.providers.kiwoom.chart_epoch", lambda: clock["now"])

    def daily_fetch(code, interval, lookback_extra):
        provider.calls += 1
        current_day = (clock["now"] // 86_400) * 86_400
        return [Bar(time=current_day, open=1, high=2, low=1, close=2, volume=1)]

    provider._fetch_bars = daily_fetch
    provider.get_bars("005930", DAY_INTERVAL, 119)
    monkeypatch.setattr("app.providers.kiwoom.time.time", lambda: 99_999_999_999)
    provider.get_bars("005930", DAY_INTERVAL, 119)
    assert provider.calls == 1

    clock["now"] += 86_400
    provider.get_bars("005930", DAY_INTERVAL, 119)
    assert provider.calls == 2


def test_daily_without_today_uses_short_cache(provider, monkeypatch):
    """장전 응답은 고정하지 않아 장 시작 뒤 오늘 봉을 다시 조회할 수 있다."""
    day = (1_788_000_000 // 86_400) * 86_400
    monkeypatch.setattr("app.providers.kiwoom.chart_epoch", lambda: day + 8 * 3600)
    provider._fetch_bars = lambda *args: (
        setattr(provider, "calls", provider.calls + 1)
        or [Bar(time=day - 86_400, open=1, high=2, low=1, close=2, volume=1)]
    )
    times = iter([100.0, 104.1, 104.1])
    monkeypatch.setattr("app.providers.kiwoom.time.time", lambda: next(times))
    provider.get_bars("005930", DAY_INTERVAL, 119)
    provider.get_bars("005930", DAY_INTERVAL, 119)
    assert provider.calls == 2
