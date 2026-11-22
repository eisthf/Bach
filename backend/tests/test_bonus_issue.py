"""무상증자 공시 감시 → 자동 매수 (app/bonus_issue.py)."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

import pytest

from app.bonus_issue import (BonusSettings, BonusTradeManager, BonusWatcher, Disclosure, classify,
                             ex_notice_of, parse_terms, target_time)
from app.market_clock import KST, chart_epoch
from app.models import Bar, OrderResult, Position


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
    assert (s.enabled, s.budget, s.order_mode, s.take_profit_pct, s.stop_pct, s.open_max_pct,
            s.take_profit_partial, s.trail_pct) == (False, 1_000_000, "expected", 20.0, 15.0, 10.0, False, 5.0)
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
    on_disclosure = bt.on_disclosure

    async def with_terms(d, ratio=1.0):
        # DART 본문(1주당 배정)이 감지와 함께 읽힌 것으로 둔다. 배정 기준 시험은 on_disclosure 를 직접 쓴다.
        job = await on_disclosure(d)
        if job is not None:
            bt.apply_terms(d.code, {"ratio": ratio})
        return job

    bt.on_disclosure = with_terms
    bt.raw_on_disclosure = on_disclosure
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


async def test_paid_too_skipped_by_default_but_card_kept_manual(bonus):
    hub, mgr, clock, bt = bonus
    bt.settings.enabled = True
    bt._now = lambda: kst("2026-10-26 10:01")
    assert await bt.on_disclosure(disclosure(kst("2026-10-26 10:00"), paid=True)) is None
    item = hub.manual_trades.items["005930"]
    assert item.origin == "bonus" and not item.stop.enabled and not item.take_profit.enabled
    assert bt.jobs["005930"].status == "skipped" and "유무상" in bt.jobs["005930"].message


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
        True, 15.0, True, 20.0)
    assert bt.jobs["005930"].kind == "intraday"
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
        jobs = {}

        async def on_disclosure(self, d):
            got.append(d.code)

        def wants_ex_notice(self):
            return False

        def apply_ex_notice(self, n):
            pass

    class H:
        account = "mock"
        bonus_trades = Bt()

    asked = []
    rows = [row(t="2026-10-23 17:00", code5="11111", acpt="1"),   # 금요일 장후 — 월요일 아침에도 넘긴다
            row(t="2026-10-26 07:58", code5="22222", acpt="2"),
            row(title="[정정]무상증자결정", t="2026-10-26 07:59", code5="33333", acpt="3")]

    def fetch(start, end, keyword="무상증자"):
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
    item = hub.manual_trades.items["005930"]   # 카드는 '수동'으로 올린다
    assert not item.stop.enabled and not item.take_profit.enabled


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
    bt.apply_terms(job.code, {"ratio": 1.0})
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


# ---------------------------------------------------------------- 시가 단일가(장전·장후 공시)
async def test_open_auction_buys_within_max_pct(bonus, monkeypatch):
    hub, mgr, clock, bt = bonus
    clock.open()
    bt.settings = BonusSettings(enabled=True, order_mode="market", open_max_pct=10)
    bt._now = lambda: kst("2026-10-26 08:59:45")   # 장전 공시, 09:00 시가 20초 전이 지났다
    quote = hub.data.auction_quote("005930")
    monkeypatch.setattr(hub.data, "auction_quote",
                        lambda code: {**quote, "expected": quote["base"] * 1.10})
    job = await bt.on_disclosure(disclosure(kst("2026-10-26 07:50")))
    assert job.kind == "open"
    await wait_status(job, "sent")
    assert job.basis == "예상체결가"   # 시장가 설정이어도 시가 예상가를 확인하고 낸다


async def test_open_auction_skips_above_max_pct(bonus, monkeypatch):
    hub, mgr, clock, bt = bonus
    clock.open()
    bt.settings = BonusSettings(enabled=True, order_mode="expected", open_max_pct=10)
    bt._now = lambda: kst("2026-10-27 08:59:45")
    quote = hub.data.auction_quote("005930")
    monkeypatch.setattr(hub.data, "auction_quote",
                        lambda code: {**quote, "expected": quote["base"] * 1.11})
    # 장중 공시인데 재개가 15:20을 넘는다 → 다음 날 시가 단일가(장전 공시처럼)
    job = await bt.on_disclosure(disclosure(kst("2026-10-26 14:50")))
    assert job.kind == "open"
    await wait_status(job, "skipped")
    assert "상한 +10%" in job.message
    item = hub.manual_trades.items["005930"]
    assert not item.stop.enabled and not item.take_profit.enabled
    assert hub.broker.position("005930").quantity == 0


def test_old_default_exits_migrate(mock_hub, tmp_path):
    import json
    hub, mgr, clock = mock_hub
    bt = hub.bonus_trades
    bt._path = tmp_path / "b.json"
    bt._path.write_text(json.dumps({"settings": {"enabled": True, "take_profit_pct": 25.0,
                                                 "stop_pct": 10.0}}), encoding="utf-8")
    bt.restore()
    assert (bt.settings.take_profit_pct, bt.settings.stop_pct, bt.settings.enabled) == (20.0, 15.0, True)
    bt._path.write_text(json.dumps({"version": 2, "settings": {"take_profit_pct": 25.0, "stop_pct": 10.0}}),
                        encoding="utf-8")
    bt.restore()
    assert (bt.settings.take_profit_pct, bt.settings.stop_pct) == (25.0, 10.0)


# ---------------------------------------------------------------- 카드: 수동·갭상승·옮기기
async def manual_card(bt, code="005930"):
    bt.settings = BonusSettings(enabled=True, order_mode="after_resume")
    bt._now = lambda: kst("2026-10-26 10:01")
    job = await bt.on_disclosure(disclosure(kst("2026-10-26 10:00"), code=code))
    return job


async def test_to_manual_cancels_waiting_job_and_exits(bonus):
    hub, mgr, clock, bt = bonus
    job = await manual_card(bt)
    assert hub.manual_trades.items["005930"].stop.enabled
    bt.to_manual("005930")
    assert job.status == "cancelled"
    it = hub.manual_trades.items["005930"]
    assert not it.stop.enabled and not it.take_profit.enabled
    assert it.stop.pct == 15.0          # 값은 남긴다(다시 켤 때 보인다)
    assert it.origin == "bonus"


def day_bar(d: str, o: float, c: float) -> Bar:
    return Bar(time=chart_epoch(kst(d + " 09:00").replace(hour=0)), open=o, high=max(o, c),
               low=min(o, c), close=c)


async def test_gap_mode_requires_manual_and_buys_on_gap(bonus, monkeypatch):
    hub, mgr, clock, bt = bonus
    await manual_card(bt)
    with pytest.raises(ValueError):
        bt.set_gap("005930", {})          # 매수 대기 중
    bt.to_manual("005930")
    bt._now = lambda: kst("2026-10-26 15:00")   # 켠 날 D = 10-26
    bt.GAP_STEP_SEC = 0.01
    bars = {"v": [day_bar("2026-10-26", 10_000, 10_500), day_bar("2026-10-27", 10_600, 10_700)]}
    monkeypatch.setattr(hub.data, "get_bars", lambda code, interval, *a: bars["v"])
    bt.set_gap("005930", {"budget": 500_000, "gap_pct": 1,
                          "take_profit": {"mode": "pct", "pct": 10, "partial": True},
                          "stop": {"mode": "pct", "pct": 3}})
    job = bt.jobs["005930"]
    assert job.gap["started"] == "2026-10-26"
    clock.open()
    await asyncio.sleep(0.05)
    assert hub.broker.position("005930").quantity == 0    # D 당일엔 판정하지 않는다
    # D+1: 시가 10,600 ≥ 10,500 × 1.01 = 10,605? 아니다 → 미달, 다음 날 다시
    bt._now = lambda: kst("2026-10-27 09:00:30")
    end = time.monotonic() + 3
    while not (job.gap or {}).get("checked") and time.monotonic() < end:
        await asyncio.sleep(0.01)
    assert job.gap["checked"] == "2026-10-27" and "미달" in job.gap["message"]
    assert hub.broker.position("005930").quantity == 0
    # D+2: 시가 10,900 ≥ 10,700 × 1.01 = 10,807 → 매수, 갭상승용 손절·익절 감시로
    bars["v"] = bars["v"] + [day_bar("2026-10-28", 10_900, 11_000)]
    bt._now = lambda: kst("2026-10-28 09:00:30")
    end = time.monotonic() + 3
    while job.gap and time.monotonic() < end:
        await asyncio.sleep(0.01)
    assert job.gap is None and "갭상승 매수" in job.message
    assert hub.broker.position("005930").quantity > 0
    it = hub.manual_trades.items["005930"]
    assert (it.stop.enabled, it.stop.pct, it.take_profit.enabled, it.take_profit.pct,
            it.take_profit.partial) == (True, 3.0, True, 10.0, True)


async def test_gap_bull_open_reference(bonus, monkeypatch):
    hub, mgr, clock, bt = bonus
    await manual_card(bt)
    bt.to_manual("005930")
    bt._now = lambda: kst("2026-10-26 15:00")
    bt.GAP_STEP_SEC = 0.01
    # 전일 양봉(시가 10,000 → 종가 10,500). 시가 10,200: 종가 기준이면 미달, 양봉 시가 기준이면 10,100 이상 → 매수
    monkeypatch.setattr(hub.data, "get_bars", lambda code, interval, *a: [
        day_bar("2026-10-26", 10_000, 10_500), day_bar("2026-10-27", 10_200, 10_300)])
    bt.set_gap("005930", {"gap_pct": 1, "bull_open": True})
    clock.open()
    bt._now = lambda: kst("2026-10-27 09:00:30")
    job = bt.jobs["005930"]
    end = time.monotonic() + 3
    while job.gap and time.monotonic() < end:
        await asyncio.sleep(0.01)
    assert job.gap is None and "전일(양봉) 시가" in job.message


async def test_move_to_manual_page_and_zone(bonus):
    hub, mgr, clock, bt = bonus
    await manual_card(bt)
    await bt.move_to_manual_page("005930")
    it = hub.manual_trades.items["005930"]
    assert it.origin == "from_bonus" and bt.jobs["005930"].status == "cancelled"
    with pytest.raises(KeyError):        # 더는 무상증자 카드가 아니다
        bt.to_manual("005930")

    await manual_card(bt, code="000660")
    hub.broker.position = lambda code: Position(code=code, quantity=3, avg_price=100.0)
    with pytest.raises(ValueError):      # 보유가 있으면 구간 매매로 못 옮긴다
        await bt.move_to_zone("000660")
    hub.broker.position = lambda code: Position(code=code)
    await bt.move_to_zone("000660")
    assert "000660" not in hub.manual_trades.items
    assert hub.candle_trades.items["000660"].engine.kind == "zone"


# ---------------------------------------------------------------- 권리락 일정
def test_parse_terms_and_ex_notice_filter():
    text = ("1. 신주의 종류와 수 보통주식 (주) 1,000 ... 1주당 신주배정 주식수 보통주식 (주) 2 "
            "신주배정기준일 2026년 03월 03일 ... 신주의 상장 예정일 2026년 03월 27일")
    assert parse_terms(text) == {"ratio": 2.0, "record_date": "2026-03-03", "listing_date": "2026-03-27"}
    assert ex_notice_of({"time": "2026-02-26 15:51", "title": "권리락 기준가격 안내",
                         "code5": "48037", "corp": "씨케이솔루션"})["code"] == "480370"
    assert ex_notice_of({"time": "2026-02-26 15:51", "title": "권리락(유상증자)", "code5": "48037"}) is None
    assert ex_notice_of({"time": "2026-02-26 15:51", "title": "무상증자결정", "code5": "48037"}) is None


async def test_ex_date_from_terms_then_notice(bonus):
    hub, mgr, clock, bt = bonus
    await manual_card(bt)
    bt.apply_terms("005930", {"ratio": 2, "record_date": "2026-11-17", "listing_date": "2026-12-08"})
    job = bt.jobs["005930"]
    assert (job.ex_date, job.ex_confirmed) == ("2026-11-16", False)   # 기준일 직전 평일(예정)
    bt.apply_ex_notice({"code": "005930", "at": "2026-11-12T16:50+09:00", "title": "권리락(무상증자)"})
    assert (job.ex_date, job.ex_confirmed) == ("2026-11-13", True)    # 안내 다음 평일(휴장일이면 안내가 맞다)
    bt.apply_terms("005930", {"ratio": 2, "record_date": "2026-11-17"})
    assert job.ex_date == "2026-11-13"                                 # 확정값은 덮지 않는다


async def test_watcher_routes_ex_notices(tmp_path):
    got = []

    class Bt:
        jobs = {}

        async def on_disclosure(self, d):
            pass

        def wants_ex_notice(self):
            return True

        def apply_ex_notice(self, n):
            got.append((n["code"], n["title"]))

    class H:
        account = "mock"
        bonus_trades = Bt()

    calls = []

    def fetch(start, end, keyword="무상증자"):
        calls.append(keyword)
        if keyword == "권리락":
            return [row(title="권리락 기준가격 안내", code5="48037", acpt="9")]
        return [row(title="권리락(무상증자)", code5="37317", acpt="8")]

    w = BonusWatcher(_Mgr({"mock": H()}), fetch=fetch, path=tmp_path / "w.json")
    await w.poll_once(kst("2026-10-26 17:00"))
    assert calls == ["무상증자", "권리락"]
    assert set(got) == {("373170", "권리락(무상증자)"), ("480370", "권리락 기준가격 안내")}
    await w.poll_once(kst("2026-10-26 17:01"))
    assert calls == ["무상증자", "권리락", "무상증자"]   # '권리락' 검색은 몇 번에 한 번


# ---------------------------------------------------------------- 1주당 배정 기준(min_ratio)
def test_min_ratio_setting():
    s = BonusSettings.parse({})
    assert s.min_ratio == 1.0
    assert BonusSettings.parse({"min_ratio": 0}).min_ratio == 0
    with pytest.raises(ValueError):
        BonusSettings.parse({"min_ratio": 11})
    bt = BonusTradeManager.__new__(BonusTradeManager)
    bt.settings = BonusSettings.parse({"min_ratio": 0.5})
    assert "1주당 배정 0.5주 이상만" in bt.describe()


async def test_ratio_below_minimum_skips_as_soon_as_terms_arrive(bonus):
    """1주당 0.5주 배정 < 기준 1주 → 주문 시각을 기다리지 않고 '매수 안 함', 카드는 수동으로 남는다."""
    hub, mgr, clock, bt = bonus
    bt.settings = BonusSettings(enabled=True, order_mode="expected")
    bt._now = lambda: kst("2026-10-26 10:01")                   # 목표 10:40 — 아직 주문 전
    job = await bt.raw_on_disclosure(disclosure(kst("2026-10-26 10:00")))
    assert job.status == "waiting"
    bt.apply_terms("005930", {"ratio": 0.5, "record_date": "2026-11-10"})
    assert job.status == "skipped" and "0.5주 < 기준 1주" in job.message
    item = hub.manual_trades.items["005930"]
    assert item.origin == "bonus" and not item.stop.enabled and not item.take_profit.enabled
    await asyncio.sleep(0.05)
    assert hub.broker.position("005930").quantity == 0


async def test_order_waits_for_ratio_then_buys(bonus):
    """시장가(감지 즉시) 방식이어도 배정 비율을 확인할 때까지 주문하지 않는다."""
    hub, mgr, clock, bt = bonus
    bt.settings = BonusSettings(enabled=True, order_mode="market")
    bt._now = lambda: kst("2026-10-26 10:01")
    job = await bt.raw_on_disclosure(disclosure(kst("2026-10-26 10:00")))
    await asyncio.sleep(0.05)
    assert job.status == "waiting" and "1주당 배정 확인 대기" in job.message
    assert hub.broker.position("005930").quantity == 0
    bt.apply_terms("005930", {"ratio": 1.0})
    await wait_status(job, "sent")
    assert hub.broker.position("005930").quantity > 0


async def test_unreadable_terms_skip(bonus):
    hub, mgr, clock, bt = bonus
    bt.settings = BonusSettings(enabled=True, order_mode="market")
    bt._now = lambda: kst("2026-10-26 10:01")
    job = await bt.raw_on_disclosure(disclosure(kst("2026-10-26 10:00")))
    bt.terms_failed("005930")
    assert job.status == "skipped" and "확인하지 못함" in job.message

    # 본문은 찾았지만 배정 비율을 못 읽은 경우도 같다
    hub.manual_trades.items.pop("005930")
    bt.jobs.pop("005930")
    job = await bt.raw_on_disclosure(disclosure(kst("2026-10-26 10:00")))
    bt.apply_terms("005930", {"record_date": "2026-11-10"})
    assert job.status == "skipped"


async def test_min_ratio_zero_does_not_wait_for_terms(bonus):
    hub, mgr, clock, bt = bonus
    bt.settings = BonusSettings(enabled=True, order_mode="market", min_ratio=0)
    bt._now = lambda: kst("2026-10-26 10:01")
    job = await bt.raw_on_disclosure(disclosure(kst("2026-10-26 10:00")))
    await wait_status(job, "sent")


async def test_ratio_above_minimum_buys(bonus):
    hub, mgr, clock, bt = bonus
    bt.settings = BonusSettings(enabled=True, order_mode="market", min_ratio=2)
    bt._now = lambda: kst("2026-10-26 10:01")
    job = await bt.raw_on_disclosure(disclosure(kst("2026-10-26 10:00")))
    bt.apply_terms("005930", {"ratio": 3.0})
    await wait_status(job, "sent")


async def test_simulated_disclosure_uses_given_ratio(bonus, tmp_path):
    hub, mgr, clock, bt = bonus
    bt.settings = BonusSettings(enabled=True, order_mode="market")
    bt._now = lambda: datetime.now(KST)
    bt._demo = lambda: True
    w = BonusWatcher(_Mgr({hub.account: hub}), fetch=lambda start, end, keyword="무상증자": [],
                     path=tmp_path / "w.json")
    await w.simulate("005930", "삼성전자", ratio=0.5)
    job = bt.jobs["005930"]
    assert job.ratio == 0.5 and job.status == "skipped"


async def test_cancel_endpoint_runs_on_event_loop(bonus, monkeypatch):
    """[취소] API: 매수 작업(asyncio.Task)을 취소하므로 async 엔드포인트여야 한다(스레드풀이면 500)."""
    import inspect

    from app import main

    hub, mgr, clock, bt = bonus
    assert inspect.iscoroutinefunction(main.bonus_cancel)
    bt.settings = BonusSettings(enabled=True, order_mode="expected")
    bt._now = lambda: kst("2026-10-26 10:01")
    job = await bt.on_disclosure(disclosure(kst("2026-10-26 10:00")))
    monkeypatch.setattr(main, "_hub", lambda account: hub)
    out = await main.bonus_cancel("mock", "005930")
    assert job.status == "cancelled" and out["jobs"][0]["status"] == "cancelled"
