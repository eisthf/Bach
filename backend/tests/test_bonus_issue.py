"""무상증자 공시 감시 → 자동 매수 (app/bonus_issue.py)."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

import pytest

from app.bonus_issue import (BonusSettings, BonusTradeManager, BonusWatcher, Disclosure, classify,
                             target_time)
from app.market_clock import KST
from app.models import OrderResult


def kst(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=KST)


def row(title="무상증자결정", t="2026-10-26 10:00", code5="12345", mkt="ko", acpt="1"):
    return {"time": t, "corp": "테스트", "title": title, "acptno": acpt, "code5": code5, "mkt": mkt}


# ---------------------------------------------------------------- 분류·시각
def test_classify_filters():
    d = classify(row())
    assert d.code == "123450" and d.market == "KOSDAQ" and d.eligible and not d.paid_too
    assert classify(row(title="[정정]무상증자결정")) is None
    assert classify(row(title="[기재정정]무상증자결정")) is None
    assert classify(row(title="권리락(무상증자)")) is None
    assert classify(row(title="유무상증자결정")).paid_too
    assert not classify(row(title="(종속회사의주요경영사항)무상증자결정")).eligible
    assert not classify(row(mkt="ne")).eligible


def test_target_time():
    # 장중 → 40분 뒤, 재개가 15:20 넘으면 다음 평일 09:00, 장전 → 그날 09:00, 금요일 장후 → 월요일
    assert target_time(kst("2026-10-26 10:00"), 40) == kst("2026-10-26 10:40")
    assert target_time(kst("2026-10-26 14:50"), 40) == kst("2026-10-27 09:00")
    assert target_time(kst("2026-10-26 07:50"), 40) == kst("2026-10-26 09:00")
    assert target_time(kst("2026-10-30 17:00"), 40) == kst("2026-11-02 09:00")


def test_settings_validation():
    s = BonusSettings.parse({})
    assert (s.enabled, s.budget, s.order_mode, s.take_profit_pct, s.stop_pct) == (
        False, 1_000_000, "expected", 25.0, 10.0)
    with pytest.raises(ValueError):
        BonusSettings.parse({"order_mode": "x"})
    with pytest.raises(ValueError):
        BonusSettings.parse({"stop_pct": 0})
    assert BonusSettings.parse({"budget": 500000}, s).order_mode == "expected"


# ---------------------------------------------------------------- 계좌별 매수
@pytest.fixture
def bonus(mock_hub):
    hub, mgr, clock = mock_hub
    bt = hub.bonus_trades
    bt.persist = lambda: None
    bt._demo = lambda: False  # 실제 요일·장 시간 규칙으로 시험한다
    bt.RETRY_SEC = 0.01
    bt.STEP_SEC = 0.01
    yield hub, mgr, clock, bt
    for job in bt.jobs.values():
        if job.task:
            job.task.cancel()


def disclosure(at: datetime, code="005930", paid=False) -> Disclosure:
    return Disclosure(acptno=f"A{at:%H%M%S}", disclosed_at=at.isoformat(), corp="삼성전자",
                      title="유무상증자결정" if paid else "무상증자결정", code=code, market="KOSPI",
                      paid_too=paid)


async def wait_status(job, *statuses, timeout=3.0):
    # 반복 횟수가 아니라 실제 시간으로 기다린다 — Windows 에선 sleep(0.01)이 거의 바로 돌아온다.
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if job.status in statuses:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"status={job.status} message={job.message}")


async def test_disabled_account_skips(bonus):
    hub, mgr, clock, bt = bonus
    assert await bt.on_disclosure(disclosure(kst("2026-10-26 10:00"))) is None
    assert "005930" not in hub.manual_trades.items
    assert any("꺼져" in t for t in mgr.logs())


async def test_paid_too_skipped_by_default(bonus):
    hub, mgr, clock, bt = bonus
    bt.settings.enabled = True
    assert await bt.on_disclosure(disclosure(kst("2026-10-26 10:00"), paid=True)) is None
    assert "005930" not in hub.manual_trades.items


async def test_upper_limit_mode_buys_within_budget_with_exits(bonus):
    hub, mgr, clock, bt = bonus
    bt.settings = BonusSettings(enabled=True, order_mode="upper_limit")
    now = kst("2026-10-26 10:01")
    bt._now = lambda: now
    job = await bt.on_disclosure(disclosure(kst("2026-10-26 10:00")))
    await wait_status(job, "sent")
    item = hub.manual_trades.items["005930"]
    assert item.origin == "bonus"
    assert (item.stop.enabled, item.stop.pct, item.take_profit.enabled, item.take_profit.pct) == (
        True, 10.0, True, 25.0)
    pos = hub.broker.position("005930")
    assert pos.quantity == job.qty > 0
    upper = hub.data.auction_quote("005930")["upper"]
    assert job.qty == int(1_000_000 // upper)


async def test_retry_until_order_accepted(bonus, monkeypatch):
    hub, mgr, clock, bt = bonus
    bt.settings = BonusSettings(enabled=True, order_mode="market")
    bt._now = lambda: kst("2026-10-26 10:01")
    calls = []
    real_buy = hub.broker.buy

    def buy(code, amount):
        calls.append(amount)
        if len(calls) < 3:  # 매매정지 중 거부
            return OrderResult(ok=False, code=code, side="buy", filled_qty=0, price=0,
                               message="매매거래정지 종목")
        return real_buy(code, amount)

    monkeypatch.setattr(hub.broker, "buy", buy)
    job = await bt.on_disclosure(disclosure(kst("2026-10-26 10:00")))
    await wait_status(job, "sent")
    assert len(calls) == 3 and job.attempts == 3


async def test_retry_does_not_double_buy_when_account_has_position(bonus, monkeypatch):
    hub, mgr, clock, bt = bonus
    bt.settings = BonusSettings(enabled=True, order_mode="market")
    bt._now = lambda: kst("2026-10-26 10:01")
    calls = []
    real_buy = hub.broker.buy

    def buy(code, amount):
        # 주문은 실제로 들어갔는데 응답이 실패로 온 경우
        calls.append(amount)
        real_buy(code, amount)
        return OrderResult(ok=False, code=code, side="buy", filled_qty=0, price=0, message="요청 실패")

    monkeypatch.setattr(hub.broker, "buy", buy)
    job = await bt.on_disclosure(disclosure(kst("2026-10-26 10:00")))
    await wait_status(job, "sent")
    assert len(calls) == 1


async def test_expected_mode_orders_before_target_with_expected_price(bonus):
    hub, mgr, clock, bt = bonus
    clock.open()
    bt.settings = BonusSettings(enabled=True, order_mode="expected", lead_sec=20)
    # 목표(10:40) 20초 전이 이미 지났다 → 곧바로 예상체결가로 주문
    bt._now = lambda: kst("2026-10-26 10:39:45")
    job = await bt.on_disclosure(disclosure(kst("2026-10-26 10:00")))
    await wait_status(job, "sent")
    assert job.basis == "예상체결가"
    assert job.qty * job.price <= 1_000_000


async def test_skip_above_pct(bonus, monkeypatch):
    hub, mgr, clock, bt = bonus
    clock.open()
    bt.settings = BonusSettings(enabled=True, order_mode="expected", skip_above_pct=5)
    bt._now = lambda: kst("2026-10-26 10:39:45")
    quote = hub.data.auction_quote("005930")
    monkeypatch.setattr(hub.data, "auction_quote",
                        lambda code: {**quote, "expected": quote["base"] * 1.2})
    job = await bt.on_disclosure(disclosure(kst("2026-10-26 10:00")))
    await wait_status(job, "skipped")
    assert hub.broker.position("005930").quantity == 0


async def test_deadline_fails_job(bonus, monkeypatch):
    hub, mgr, clock, bt = bonus
    bt.settings = BonusSettings(enabled=True, order_mode="market")
    clock_now = {"t": kst("2026-10-26 10:01")}
    bt._now = lambda: clock_now["t"]
    monkeypatch.setattr(hub.broker, "buy", lambda c, a: OrderResult(
        ok=False, code=c, side="buy", filled_qty=0, price=0, message="매매거래정지 종목"))
    job = await bt.on_disclosure(disclosure(kst("2026-10-26 10:00")))
    await asyncio.sleep(0.05)
    clock_now["t"] = kst("2026-10-26 15:31")  # 계속 거부되다 장 마감
    await wait_status(job, "failed")
    assert "기한" in job.message


async def test_cancel_waiting_job(bonus):
    hub, mgr, clock, bt = bonus
    bt.settings = BonusSettings(enabled=True, order_mode="after_resume")
    bt._now = lambda: kst("2026-10-26 10:01")
    job = await bt.on_disclosure(disclosure(kst("2026-10-26 10:00")))
    await asyncio.sleep(0.05)
    assert job.status == "waiting"
    bt.cancel("005930")
    assert job.status == "cancelled"
    await asyncio.sleep(0.05)
    assert hub.broker.position("005930").quantity == 0


async def test_restore_resumes_waiting_job(bonus, tmp_path):
    hub, mgr, clock, bt = bonus
    bt.settings = BonusSettings(enabled=True, order_mode="after_resume")
    bt._now = lambda: kst("2026-10-26 10:01")
    del bt.persist  # 실제 저장
    job = await bt.on_disclosure(disclosure(kst("2026-10-26 10:00")))
    await bt.close()
    other = BonusTradeManager(hub)
    other._now, other._demo = bt._now, bt._demo
    other.restore()
    try:
        assert other.settings.enabled and other.jobs["005930"].status == "waiting"
        assert other.jobs["005930"].task is not None
    finally:
        await other.close()


# ---------------------------------------------------------------- 감시
class _Mgr:
    def __init__(self, hubs):
        self.hubs = hubs
        self.msgs = []

    def broadcast(self, msg):
        self.msgs.append(msg)

    def _log(self, text):
        self.msgs.append({"type": "log", "text": text})


async def test_watcher_dispatches_new_disclosures_once(tmp_path):
    got = []

    class Bt:
        async def on_disclosure(self, d):
            got.append(d.code)

    class H:
        account = "mock"
        bonus_trades = Bt()

    asked = []
    rows = [row(t="2026-10-23 17:00", code5="11111", acpt="1"),   # 금요일 장후 — 월요일 아침에도 넘긴다
            row(t="2026-10-26 07:58", code5="22222", acpt="2"),
            row(title="[정정]무상증자결정", t="2026-10-26 07:59", code5="33333", acpt="3")]

    def fetch(start, end):
        asked.append((start.isoformat(), end.isoformat()))
        return rows

    w = BonusWatcher(_Mgr({"mock": H()}), fetch=fetch, path=tmp_path / "w.json")
    now = kst("2026-10-26 08:00")  # 월요일
    await w.poll_once(now)
    assert asked == [("2026-10-23", "2026-10-26")]  # 직전 평일(금)~오늘
    assert got == ["111110", "222220"]
    await w.poll_once(now)  # 같은 접수번호는 다시 처리하지 않는다
    assert got == ["111110", "222220"]
    # 저장된 접수번호는 재시작 뒤에도 기억한다
    w2 = BonusWatcher(_Mgr({}), fetch=fetch, path=tmp_path / "w.json")
    assert set(w2.seen) == {"1", "2"}


def test_search_range():
    from datetime import date
    assert BonusWatcher.search_range(date(2026, 10, 27)) == (date(2026, 10, 26), date(2026, 10, 27))
    assert BonusWatcher.search_range(date(2026, 10, 26)) == (date(2026, 10, 23), date(2026, 10, 26))


async def test_skip_when_target_already_passed(bonus):
    hub, mgr, clock, bt = bonus
    bt.settings = BonusSettings(enabled=True, order_mode="market")
    # 10:00 공시를 10:45에 처음 봤다(서버가 꺼져 있었음) — 10:40 재개가 이미 지났다
    bt._now = lambda: kst("2026-10-26 10:45")
    assert await bt.on_disclosure(disclosure(kst("2026-10-26 10:00"))) is None
    job = bt.jobs["005930"]
    assert job.status == "skipped" and "지남" in job.message
    assert "005930" not in hub.manual_trades.items


async def test_late_detection_before_target_still_buys(bonus):
    hub, mgr, clock, bt = bonus
    bt.settings = BonusSettings(enabled=True, order_mode="market")
    # 10:00 공시를 10:25에 봤다 — 아직 정지 중이라 공시 직후 감지와 같은 매수
    bt._now = lambda: kst("2026-10-26 10:25")
    job = await bt.on_disclosure(disclosure(kst("2026-10-26 10:00")))
    await wait_status(job, "sent")
    # 금요일 장후 공시를 월요일 08:00에 봤다 — 목표는 월요일 09:00
    bt._now = lambda: kst("2026-10-26 08:00")
    job2 = await bt.on_disclosure(disclosure(kst("2026-10-23 17:00"), code="000660"))
    assert job2 is not None and job2.target_at == kst("2026-10-26 09:00").isoformat()


async def test_watcher_fetch_error_reported_once(tmp_path):
    def boom(start, end):
        raise RuntimeError("down")

    mgr = _Mgr({})
    w = BonusWatcher(mgr, fetch=boom, path=tmp_path / "w.json")
    await w.poll_once(kst("2026-10-26 10:00"))
    await w.poll_once(kst("2026-10-26 10:00:10"))
    logs = [m["text"] for m in mgr.msgs if m.get("type") == "log"]
    assert len([t for t in logs if "조회 실패" in t]) == 1
    assert w.status["ok"] is False


async def test_demo_clock_ignores_weekday_and_hours(mock_hub):
    hub, mgr, clock = mock_hub
    bt = hub.bonus_trades
    bt.persist = lambda: None
    bt.RETRY_SEC = bt.STEP_SEC = 0.01
    bt.settings = BonusSettings(enabled=True, order_mode="market", resume_min=1)
    sunday = kst("2026-10-25 21:00")
    bt._now = lambda: sunday
    job = await bt.on_disclosure(disclosure(sunday))
    try:
        assert job.target_at == kst("2026-10-25 21:01").isoformat()
        await wait_status(job, "sent")
    finally:
        await bt.close()


# ---------------------------------------------------------------- 09:00 [상한가 매매] 보호
def test_watcher_quiet_around_open(tmp_path):
    w = BonusWatcher(_Mgr({}), fetch=lambda start, end: [], path=tmp_path / "w.json")
    assert w.should_poll(kst("2026-10-26 08:59:29"))
    assert not w.should_poll(kst("2026-10-26 08:59:30"))
    assert not w.should_poll(kst("2026-10-26 09:01:29"))
    assert w.should_poll(kst("2026-10-26 09:01:30"))
    assert not w.should_poll(kst("2026-10-25 10:00"))  # 일요일


async def test_watcher_broadcasts_only_on_change(tmp_path):
    rows = []
    fail = {"on": False}

    def fetch(start, end):
        if fail["on"]:
            raise RuntimeError("down")
        return rows

    mgr = _Mgr({})
    w = BonusWatcher(mgr, fetch=fetch, path=tmp_path / "w.json")
    sent = lambda: [m["type"] for m in mgr.msgs if m.get("type", "").startswith("bonus_watch")]
    now = kst("2026-10-26 10:00")
    await w.poll_once(now)
    await w.poll_once(now)
    assert sent() == []                      # 바뀐 게 없으면 방송 안 함
    rows.append(row(t="2026-10-26 09:59", acpt="9", mkt="ne"))
    await w.poll_once(now)
    assert sent() == ["bonus_watch"]         # 새 공시
    fail["on"] = True
    await w.poll_once(now)
    await w.poll_once(now)
    assert sent() == ["bonus_watch", "bonus_watch_status"]  # 오류로 바뀔 때 한 번
    fail["on"] = False
    await w.poll_once(now)
    assert sent()[-1] == "bonus_watch_status"  # 회복


async def test_buy_waits_for_ulc_first_orders(bonus, monkeypatch):
    hub, mgr, clock, bt = bonus
    bt.settings = BonusSettings(enabled=True, order_mode="market")
    bt._now = lambda: kst("2026-10-26 09:00:05")
    pending = {"on": True}
    monkeypatch.setattr(hub, "ulc_first_orders_pending", lambda: pending["on"])
    calls = []
    real_buy = hub.broker.buy
    monkeypatch.setattr(hub.broker, "buy", lambda c, a: calls.append(c) or real_buy(c, a))
    job = await bt.on_disclosure(disclosure(kst("2026-10-26 09:00")))
    await asyncio.sleep(0.1)
    assert calls == [] and job.status == "waiting"
    assert any("상한가 매매 1차 주문을 먼저" in t for t in mgr.logs())
    pending["on"] = False
    await wait_status(job, "sent")
    assert calls == ["005930"]


async def test_ulc_yield_has_upper_bound(bonus, monkeypatch):
    hub, mgr, clock, bt = bonus
    bt.settings = BonusSettings(enabled=True, order_mode="market")
    bt._now = lambda: kst("2026-10-26 09:00:05")
    bt.ULC_YIELD_MAX_SEC = 0.05
    monkeypatch.setattr(hub, "ulc_first_orders_pending", lambda: True)
    job = await bt.on_disclosure(disclosure(kst("2026-10-26 09:00")))
    await wait_status(job, "sent")


def test_hub_ulc_first_orders_pending(mock_hub):
    hub, mgr, clock = mock_hub
    assert hub.ulc_first_orders_pending() is False
