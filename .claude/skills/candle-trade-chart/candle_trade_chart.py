"""대금 양봉·구간 매매 실매매 기록 차트 생성기.

엔진 로그(logs/events.log 의 candle_trade 이벤트) + 계좌 체결 기록(state.{계좌}.trades.json)에서
매매 한 건(등록 → 엔진 매매 → 수동 인계 뒤 포지션이 0이 될 때까지)을 묶어, 키움 차트 API
(조회 전용, 주문 없음)로 일봉·3분봉을 받고 HTML 한 장으로 만든다.

- 매매 한 건: 종목의 '등록' 메시지부터 다음 '등록' 전까지. 체결이 없는 건(기한 만료·설정 중 삭제)은 뺀다.
- 체결 범위: 첫 엔진 매수 메시지 이후. 엔진 매수·매도 메시지 순서로 사유를 붙이고, 수동 인계 뒤
  체결은 '수동'으로 포함하되 보유 수량이 0이 되거나 그 종목의 상한가(ULC) 매매가 시작되면 멈춘다.
- 일봉: 기준 캔들(대금 양봉)·매입 구간·평단·익절선 표시. 3분봉: 체결이 있었던 날들(+ 첫날 전일 60봉 예열).
- 받은 봉은 backend/backtest_data/candle_chart/ 에 캐시(gitignore). 일봉은 실행 날짜별로 캐시.

실행(backend 의 uv 환경에서):
  cd backend && uv run python ../.claude/skills/candle-trade-chart/candle_trade_chart.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SKILL = Path(__file__).resolve().parent
ROOT = SKILL.parents[2]
BACKEND = ROOT / "backend"
CACHE = BACKEND / "backtest_data" / "candle_chart"
OHLCV = ("time", "open", "high", "low", "close", "volume")
WARMUP = 60  # 3분봉 MA60 예열
KST = 9 * 3600


def parse_args():
    ap = argparse.ArgumentParser(description="대금 양봉·구간 매매 일봉·3분봉 차트(HTML) 생성")
    ap.add_argument("--account", default="real", help="계좌 id (기본 real)")
    ap.add_argument("--since", help="등록일 YYYY-MM-DD 이후(포함)")
    ap.add_argument("--until", help="등록일 YYYY-MM-DD 이전(포함)")
    ap.add_argument("--code", action="append", help="종목코드만 (여러 번 가능)")
    ap.add_argument("--kind", choices=["candle", "zone"], help="대금 양봉(candle) 또는 구간 매매(zone)만")
    ap.add_argument("--out", default=str(ROOT / "research" / "candle_trades" / "index.html"))
    ap.add_argument("--refresh", action="store_true", help="봉 캐시 무시하고 다시 조회")
    return ap.parse_args()


def trades_path(account: str) -> Path:
    return BACKEND / ("state.trades.json" if account == "mock" else f"state.{account}.trades.json")


def chart_epoch(ts: str) -> int:
    """로그 UTC 타임스탬프 → 차트 epoch(KST 벽시계를 UTC로 둔 값, 체결 time 과 같은 축)."""
    return int(datetime.fromisoformat(ts).timestamp()) + KST


def ymd(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y%m%d")


def num(s: str) -> float:
    return float(s.replace(",", ""))


def scan_log(account: str):
    """candle_trade 메시지 → 매매 건 목록, ULC 매매가 있었던 (code, day) 집합."""
    camps, ulc_days, cur = [], set(), {}
    for line in open(BACKEND / "logs" / "events.log", encoding="utf-8"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("account") != account:
            continue
        text = e.get("text", "")
        if e.get("event") == "message":
            m = re.match(r"\[(\w+)\] 자동매매 시작 \(X=", text)
            if m:
                ulc_days.add((m.group(1), ymd(chart_epoch(e["timestamp"]))))
            continue
        if e.get("event") != "candle_trade":
            continue
        m = re.match(r"\[(\w+)\] (대금양봉|구간매매): (.*)", text, re.S)
        if not m:
            continue
        code, label, body = m.groups()
        t = chart_epoch(e["timestamp"])
        if body.startswith("등록"):
            c = {"code": code, "kind": "zone" if label == "구간매매" else "candle", "reg": t,
                 "base": None, "zone": None, "tp": None, "entry_days": None,
                 "buys": [], "sells": [], "engine_from": None, "handoff": None, "end_reason": None, "log": []}
            if mm := re.search(r"기준 캔들 (\d{4}-\d\d-\d\d) 시가 ([\d,]+) 종가 ([\d,]+)", body):
                c["base"] = {"date": mm.group(1).replace("-", ""), "open": num(mm.group(2)), "close": num(mm.group(3))}
            cur[code] = c
            camps.append(c)
            continue
        c = cur.get(code)
        if c is None or body.startswith("복원"):
            continue
        c["log"].append({"time": t, "text": body})
        if mm := re.search(r"매입 구간(?: 지정)? ([\d,]+) ~ ([\d,]+)", body):
            if not c["buys"]:
                c["zone"] = [num(mm.group(1)), num(mm.group(2))]
            if mm2 := re.search(r"(\d+)거래일 안에 진입", body):
                c["entry_days"] = int(mm2.group(1))
        elif mm := re.match(r"(\d)차 매수 \d+주", body):
            c["buys"].append(f"{mm.group(1)}차")
            c["engine_from"] = c["engine_from"] or t
        elif mm := re.search(r"절반 익절: 평단 \+([\d.]+)%", body):
            c["tp"] = float(mm.group(1))
        elif body.startswith("절반 익절"):
            c["sells"].append("절반 익절")
        elif mm := re.match(r"(.+?): 전량 \d+주 매도", body):
            c["sells"].append(mm.group(1).split(" (")[0])
            c["end_reason"] = mm.group(1)
        elif body.startswith("수동매매 인계"):
            c["handoff"] = c["handoff"] or t
            c["end_reason"] = c["end_reason"] or body
        elif body.startswith("기한 만료") or body.startswith("1차 매수 실패"):
            c["end_reason"] = body
    return camps, ulc_days


class Bars:
    """키움 차트 조회(캐시 우선). 토큰은 캐시 미스가 처음 날 때만 발급."""

    def __init__(self, account: str, refresh: bool):
        self.account, self.refresh, self._token = account, refresh, None
        sys.path.insert(0, str(BACKEND))
        from dotenv import load_dotenv
        load_dotenv(BACKEND / ".env")
        from app.accounts import _account_config
        from app.providers import kiwoom_api as kw
        self.kw, self.cfg = kw, _account_config(account)
        CACHE.mkdir(parents=True, exist_ok=True)

    def token(self) -> str:
        if self._token is None:
            if self.cfg.provider != "kiwoom" or not self.cfg.appkey or not self.cfg.secret:
                raise SystemExit(f"계좌 '{self.account}' 에 키움 자격증명이 없습니다(backend/.env).")
            self._token = self.kw.fetch_access_token(self.cfg.appkey, self.cfg.secret, mock=self.cfg.kiwoom_mock).value
        return self._token

    def _cached(self, name: str, fetch):
        path = CACHE / name
        if path.exists() and not self.refresh:
            return json.loads(path.read_text(encoding="utf-8"))
        rows = fetch()
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        time.sleep(0.3)
        return rows

    def min3(self, code: str, day: str):
        return self._cached(f"{code}_{day}_3m.json", lambda: self.kw.fetch_min_bars_on(
            self.token(), code, day, tic_scope=3, mock=self.cfg.kiwoom_mock))

    def daily(self, code: str, asof: str, lookback: int):
        return self._cached(f"{code}_daily_{asof}_{lookback}.json", lambda: self.kw.fetch_day_bars(
            self.token(), code, mock=self.cfg.kiwoom_mock, today=asof, lookback_extra=lookback))

    def name(self, code: str) -> str:
        return self._cached(f"{code}_name.json", lambda: self.kw.fetch_stock_name(
            self.token(), code, mock=self.cfg.kiwoom_mock)) or code


def campaign_fills(c: dict, fills: list, ulc_days: set, next_reg: int | None) -> list:
    """이 매매 건에 속한 체결(시간순)."""
    if c["engine_from"] is None:
        return []
    start = c["engine_from"] - 120  # 매수 메시지가 체결 직전·직후로 찍힐 수 있다
    out, net = [], 0
    for f in sorted((f for f in fills if f["code"] == c["code"]), key=lambda f: f["time"]):
        if f["time"] < start or (next_reg and f["time"] >= next_reg):
            continue
        day = f["id"][:10].replace("-", "")
        if (c["code"], day) in ulc_days:
            break
        out.append(f)
        net += f["qty"] if f["side"] == "buy" else -f["qty"]
        if net <= 0:
            break
    return out


def main():
    args = parse_args()
    tpath = trades_path(args.account)
    if not tpath.exists():
        raise SystemExit(f"{tpath.name} 이 없습니다.")
    fills = json.loads(tpath.read_text(encoding="utf-8"))
    camps, ulc_days = scan_log(args.account)
    since = (args.since or "0000-00-00").replace("-", "")
    until = (args.until or "9999-99-99").replace("-", "")

    picked = []
    for i, c in enumerate(camps):
        if not since <= ymd(c["reg"]) <= until or (args.code and c["code"] not in args.code):
            continue
        if args.kind and c["kind"] != args.kind:
            continue
        nxt = next((d["reg"] for d in camps[i + 1:] if d["code"] == c["code"]), None)
        cf = campaign_fills(c, fills, ulc_days, nxt)
        if cf:
            picked.append((c, cf))
    if not picked:
        raise SystemExit("조건에 맞는 대금 양봉·구간 매매(체결 있는 건)가 없습니다.")

    bars = Bars(args.account, args.refresh)
    today = date.today()
    asof = today.strftime("%Y%m%d")
    trades, daily_out = [], {}
    for c, cf in picked:
        code = c["code"]
        first_day = ymd(min(c["reg"], cf[0]["time"]))
        if c["base"]:
            first_day = min(first_day, c["base"]["date"])
        lookback = int((today - datetime.strptime(first_day, "%Y%m%d").date()).days * 0.7) + 100
        rows = [b for b in bars.daily(code, asof, lookback) if b.get("volume", 0) > 0]

        # 주문번호로 묶고 사유 라벨: 엔진 구간(인계 전) 체결은 엔진 메시지 순서, 그 뒤는 '수동'
        groups = {}
        for f in cf:
            g = groups.setdefault(f["id"].split(":")[1], {"side": f["side"], "qty": 0, "amt": 0.0, "time": f["time"]})
            g["qty"] += f["qty"]
            g["amt"] += f["qty"] * f["price"]
            g["time"] = min(g["time"], f["time"])
        blabels, slabels = iter(c["buys"]), iter(c["sells"])
        orders = []
        for g in sorted(groups.values(), key=lambda g: g["time"]):
            engine = c["handoff"] is None or g["time"] < c["handoff"]
            label = (next(blabels if g["side"] == "buy" else slabels, None) if engine else None) or "수동"
            orders.append({**g, "price": round(g["amt"] / g["qty"], 1), "label": label, "day": ymd(g["time"])})

        side_sum = lambda side, k: sum(o[k] for o in orders if o["side"] == side)
        buy_q, sell_q = side_sum("buy", "qty"), side_sum("sell", "qty")
        buy_a, sell_a = side_sum("buy", "amt"), side_sum("sell", "amt")
        hold = buy_q - sell_q
        avg = buy_a / buy_q if buy_q else None
        last = rows[-1] if rows else None
        if hold <= 0:
            status, pnl, unreal = "청산", sell_a - buy_a, None
        else:
            # 보유 중: 실현분(매도 수량 × (매도가 − 평단)) + 최근 종가 평가
            status = "보유 중"
            pnl = sell_a - sell_q * avg
            unreal = (last["close"] - avg) * hold if last else None
        notes = []
        if c["handoff"]:
            notes.append(f"{datetime.fromtimestamp(c['handoff'], timezone.utc):%m-%d %H:%M} 수동매매 인계 — 이후 체결은 '수동'")
        if hold > 0 and last:
            notes.append(f"{hold}주 보유 중 · 평가는 최근 일봉 종가 {last['close']:,.0f}({last['_date'][4:6]}/{last['_date'][6:]}) 기준")

        # 3분봉: 체결이 있었던 날들 + 첫날 직전 거래일 정규장 60봉(MA 예열)
        fill_days = sorted({o["day"] for o in orders})
        prev_days = [b["_date"] for b in rows if b["_date"] < fill_days[0]]
        m3, day_starts = [], []
        if prev_days:
            m3 += [b for b in bars.min3(code, prev_days[-1]) if "0900" <= b["_hhmm"] <= "1527"][-WARMUP:]
        for d in fill_days:
            day_starts.append({"day": d, "idx": len(m3)})
            m3 += [b for b in bars.min3(code, d) if "0900" <= b["_hhmm"] <= "1527"]

        # 일봉은 기준일(또는 등록일) 앞 95봉부터만(MA60 예열 + 표시)
        idx = next((i for i, b in enumerate(rows) if b["_date"] >= first_day), 0)
        key = f"{code}_{len(trades)}"
        daily_out[key] = [{k: b[k] for k in OHLCV} for b in rows[max(0, idx - 95):]]

        trades.append({
            "code": code, "name": bars.name(code), "kind": c["kind"], "reg": ymd(c["reg"]),
            "base": c["base"], "zone": c["zone"], "tp": c["tp"], "entry_days": c["entry_days"],
            "end_reason": c["end_reason"], "orders": orders, "status": status,
            "buy_q": buy_q, "sell_q": sell_q, "buy_a": buy_a, "sell_a": sell_a, "hold": hold,
            "avg": avg, "pnl": pnl, "unreal": unreal, "notes": notes, "daily_key": key,
            "min3": [{k: b[k] for k in OHLCV} for b in m3], "day_starts": day_starts,
            "fills": [{k: f[k] for k in ("time", "side", "qty", "price")} for f in cf],
            "log": [x for x in c["log"] if not x["text"].startswith("체결:")],
        })
        tag = "대금양봉" if c["kind"] == "candle" else "구간매매"
        extra = "" if unreal is None else f" 평가 {unreal:+,.0f}원"
        print(f"{ymd(c['reg'])} {code} {trades[-1]['name']} {tag} {status} 실현 {pnl:+,.0f}원{extra}")

    data = {"trades": trades, "daily": daily_out, "account": args.account, "trades_file": tpath.name,
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M")}
    html = (SKILL / "template.html").read_text(encoding="utf-8").replace(
        "__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"{len(trades)}건 → {out} ({out.stat().st_size / 1024:.0f}KB)")


if __name__ == "__main__":
    main()
