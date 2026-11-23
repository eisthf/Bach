"""[상한가 매매] 익절·손절선의 시점별 기록(차트 계단선)과 예상체결가 응답 원문 로그.

평단이 바뀌면 익절선도 바뀐다. 지금 값 하나로 차트 전체에 가로선을 그으면 지난 봉을 지금 기준과
비교하는 것처럼 보이므로(2026-11-23 18064K), 기준이 바뀐 시각마다 값을 남겨 계단으로 그린다.
"""
import json

from app.strategy.ulc import UlcEngine

from conftest import plain_config
from test_ulc_auction_first_leg import _monitor, _stub

X = 99_700.0


def _engine() -> tuple[UlcEngine, dict]:
    now = {"t": 1_000}
    eng = UlcEngine(code="18064K", config=plain_config(max_buy_amount=1_000_000, ulc_tp=0.05, ulc_sl=0.05),
                    x=X, z=105_700.0, log=lambda m: None)
    eng.wall = lambda: now["t"]
    eng.setup()
    return eng, now


def test_marks_follow_avg_changes_and_stop_activation():
    eng, now = _engine()
    assert eng.view()["exit_marks"] == []                      # 보유 전엔 기록 없음

    eng.adopt_auction("A1", amount=500_000, ordered=4, bought=4, spent=4 * 105_800.0)
    now["t"] = 1_900
    eng.legs[1].order_no, eng.legs[1].ordered = "B2", 5
    eng.on_fill("buy", 5, 99_500.0, "B2")                      # 2차 체결 → 평단 102,300, 손절 켜짐

    marks = eng.view()["exit_marks"]
    assert [m["t"] for m in marks] == [1_000, 1_900]
    assert round(marks[0]["tp"]) == 111_090 and not marks[0]["stop_active"]
    assert round(marks[1]["tp"]) == 107_415 and marks[1]["stop_active"]
    assert round(marks[1]["stop"]) == 97_185


def test_same_second_overwrites_and_unchanged_does_not_append():
    eng, now = _engine()
    eng.adopt_auction("A1", amount=500_000, ordered=4, bought=4, spent=4 * 105_800.0)
    eng.on_fill("buy", 1, 100_000.0, "")                        # 같은 초의 평단 변화는 마지막 값으로
    eng.view()
    eng.view()
    assert len(eng.exit_marks) == 1


def test_marks_survive_carry_over():
    eng, _ = _engine()
    eng.adopt_auction("A1", amount=500_000, ordered=4, bought=4, spent=4 * 105_800.0)
    data = json.loads(json.dumps(eng.to_dict()))
    back = UlcEngine.from_dict("18064K", eng.config, data, log=lambda m: None)
    assert back.exit_marks == eng.exit_marks


async def test_auction_skip_logs_quote_raw(mock_hub):
    hub, mgr, clock = mock_hub
    stock = _monitor(hub)
    _stub(hub, expected=0.0)
    raw = {"return_code": 0, "exp_cntr_pric": "", "upl_pric": "+129600"}
    quote = hub.data.auction_quote("005930")
    hub.data.auction_quote = lambda code: {**quote, "raw": raw}
    logged = []
    orig = hub._log
    hub._log = lambda text, **f: (logged.append(f), orig(text, **f))
    await hub._auction_order(stock)
    skip = next(f for f in logged if f.get("event") == "auction_skip")
    assert skip["quote_raw"] == raw


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


def test_fetch_auction_quote_returns_raw(monkeypatch):
    from app.providers import kiwoom_api as kw
    body = {"return_code": 0, "base_pric": "99700", "upl_pric": "+129600", "cur_prc": "99700",
            "exp_cntr_pric": "", "exp_cntr_qty": ""}
    monkeypatch.setattr(kw, "_post", lambda *a, **k: _Resp(200, body))
    q = kw.fetch_auction_quote("T", "18064K")
    assert q["expected"] == 0 and q["upper"] == 129_600 and q["raw"] == body

    err = {"return_code": 1, "return_msg": "조회 실패"}
    monkeypatch.setattr(kw, "_post", lambda *a, **k: _Resp(200, err))
    q = kw.fetch_auction_quote("T", "18064K")
    assert q["expected"] == 0 and q["raw"]["return_msg"] == "조회 실패"
