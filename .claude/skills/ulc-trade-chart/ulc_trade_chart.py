"""상한가 따라잡기(ULC) 실매매 기록 차트 생성기.

계좌 체결 기록(state.{계좌}.trades.json) + 엔진 로그(logs/events.log)에서 ULC 매매(종목·날짜)를
찾아, 키움 차트 API(조회 전용, 주문 없음)로 3분봉·일봉을 받고 HTML 한 장으로 만든다.

- ULC 매매 판정: 그날 그 종목에 '자동매매 시작 (X=…, Z=…)' 엔진 메시지가 있고 체결이 있으면.
  대금 양봉·구간 매매(candle_trade 이벤트)와 순수 수동매매는 그 메시지가 없어 자동 제외된다.
- 체결은 주문번호로 묶고, 엔진 메시지 순서(1차/2차/3차 매수, 익절/트레일링/손절)로 사유를 붙인다.
  메시지보다 주문이 많으면 남는 주문은 '수동'.
- 3분봉 MA60 예열용으로 직전 거래일 정규장 3분봉 마지막 60개를 앞에 붙인다.
- 받은 봉은 backend/backtest_data/ulc_chart/ 에 캐시(gitignore). 일봉은 실행 날짜별로 캐시.

실행(backend 의 uv 환경에서):
  cd backend && uv run python ../.claude/skills/ulc-trade-chart/ulc_trade_chart.py
  ... --since 2026-10-01 --until 2026-10-15 --account real --out ../research/ulc_trades/index.html
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

SKILL = Path(__file__).resolve().parent
ROOT = SKILL.parents[2]
BACKEND = ROOT / "backend"
CACHE = BACKEND / "backtest_data" / "ulc_chart"
OHLCV = ("time", "open", "high", "low", "close", "volume")
WARMUP = 60  # 3분봉 MA60 예열


def parse_args():
    ap = argparse.ArgumentParser(description="ULC 실매매 3분봉·일봉 차트(HTML) 생성")
    ap.add_argument("--account", default="real", help="계좌 id (기본 real)")
    ap.add_argument("--since", help="시작일 YYYY-MM-DD (포함)")
    ap.add_argument("--until", help="종료일 YYYY-MM-DD (포함)")
    ap.add_argument("--code", action="append", help="종목코드만 (여러 번 가능)")
    ap.add_argument("--out", default=str(ROOT / "research" / "ulc_trades" / "index.html"))
    ap.add_argument("--refresh", action="store_true", help="봉 캐시 무시하고 다시 조회")
    return ap.parse_args()


def trades_path(account: str) -> Path:
    return BACKEND / ("state.trades.json" if account == "mock" else f"state.{account}.trades.json")


def scan_log(account: str) -> dict:
    """(code, YYYYMMDD) → 엔진 메시지 요약."""
    msgs = defaultdict(lambda: {"buy": [], "sell": [], "sc": None, "x": None, "z": None, "notes": []})
    log = BACKEND / "logs" / "events.log"
    for line in open(log, encoding="utf-8"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("account") != account or e.get("event") != "message":
            continue
        m = re.match(r"\[(\w+)\] (.*)", e.get("text", ""), re.S)
        if not m:
            continue
        kst = datetime.fromisoformat(e["timestamp"]) + timedelta(hours=9)
        d = msgs[(m.group(1), kst.strftime("%Y%m%d"))]
        body = m.group(2)
        if mm := re.match(r"SC(\d) 진입", body):
            d["sc"] = int(mm.group(1))
        elif mm := re.match(r"자동매매 시작 \(X=([\d,]+), Z=([\d,]+)\)", body):
            d["x"], d["z"] = (float(v.replace(",", "")) for v in mm.groups())
        elif mm := re.match(r"(\d)차 매수", body):
            d["buy"].append(f"{mm.group(1)}차")
        elif body.startswith("익절"):
            d["sell"].append("익절")
        elif body.startswith("트레일링"):
            d["sell"].append("트레일링")
        elif body.startswith("손절:"):
            d["sell"].append("손절")
        elif "PUSH → MANUAL_TRADING" in body and d["buy"]:
            d["notes"].append(f"{kst:%H:%M} 사람이 PUSH로 자동매매 중단(수동 전환)")
    return msgs


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


def main():
    args = parse_args()
    tpath = trades_path(args.account)
    if not tpath.exists():
        raise SystemExit(f"{tpath.name} 이 없습니다.")
    fills = json.loads(tpath.read_text(encoding="utf-8"))
    msgs = scan_log(args.account)
    since = (args.since or "0000-00-00").replace("-", "")
    until = (args.until or "9999-99-99").replace("-", "")

    fill_days = sorted({(f["code"], f["id"][:10].replace("-", "")) for f in fills}, key=lambda c: (c[1], c[0]))
    cases = [(c, d) for c, d in fill_days
             if msgs.get((c, d), {}).get("x") is not None and since <= d <= until
             and (not args.code or c in args.code)]
    if not cases:
        raise SystemExit("조건에 맞는 상한가(ULC) 매매가 없습니다.")

    bars = Bars(args.account, args.refresh)
    asof = date.today().strftime("%Y%m%d")
    first = datetime.strptime(cases[0][1], "%Y%m%d").date()
    # 가장 이른 매매일 앞으로 MA60 + 여유 35봉이 보이도록(달력일 → 거래일 대략 0.7배)
    lookback = int((date.today() - first).days * 0.7) + 100
    daily = {c: [b for b in bars.daily(c, asof, lookback) if b.get("volume", 0) > 0] for c in {c for c, _ in cases}}

    trades = []
    for code, day in cases:
        date_iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        day_fills = [f for f in fills if f["code"] == code and f["id"].startswith(date_iso)]
        groups = {}
        for f in day_fills:
            g = groups.setdefault(f["id"].split(":")[1], {"side": f["side"], "qty": 0, "amt": 0.0, "time": f["time"]})
            g["qty"] += f["qty"]
            g["amt"] += f["qty"] * f["price"]
            g["time"] = min(g["time"], f["time"])
        info = msgs[(code, day)]
        blabels, slabels = iter(info["buy"]), iter(info["sell"])
        orders = []
        for g in sorted(groups.values(), key=lambda g: g["time"]):
            label = next(blabels if g["side"] == "buy" else slabels, None) or "수동"
            orders.append({**g, "price": round(g["amt"] / g["qty"], 1), "label": label})
        side_sum = lambda side, k: sum(o[k] for o in orders if o["side"] == side)
        buy_q, sell_q = side_sum("buy", "qty"), side_sum("sell", "qty")
        buy_a, sell_a = side_sum("buy", "amt"), side_sum("sell", "amt")
        closed = buy_q == sell_q and buy_q > 0
        labels = [o["label"] for o in orders if o["side"] == "sell"]
        result = ("손절" if "손절" in labels else "익절+트레일링" if "트레일링" in labels else "청산") if closed else "미청산"
        notes = list(info["notes"])
        if "수동" in labels:
            notes.append("엔진 매도 외에 계좌 잔량을 수동 매도")
        if not closed:
            notes.append(f"{buy_q - sell_q}주 보유 상태로 그날 종료 — 이후 매도는 이 차트에 없음(손익 미산정)")
        m3 = [b for b in bars.min3(code, day) if b["_hhmm"] <= "1527"]
        prev_days = [b["_date"] for b in daily[code] if b["_date"] < day]
        p3 = [b for b in bars.min3(code, prev_days[-1]) if "0900" <= b["_hhmm"] <= "1527"][-WARMUP:] if prev_days else []
        trades.append({
            "code": code, "name": bars.name(code), "day": day,
            "sc": info["sc"], "x": info["x"], "z": info["z"],
            "orders": orders, "buy_q": buy_q, "sell_q": sell_q, "buy_a": buy_a, "sell_a": sell_a,
            "pnl": (sell_a - buy_a) if closed else None, "result": result, "notes": notes,
            "min3": [{k: b[k] for k in OHLCV} for b in p3 + m3], "prev_n": len(p3),
            "fills": [{k: f[k] for k in ("time", "side", "qty", "price")} for f in day_fills],
        })
        pnl = trades[-1]["pnl"]
        print(f"{day} {code} {trades[-1]['name']} SC{info['sc']} {result} {'' if pnl is None else f'{pnl:+,.0f}원'}")

    # 일봉은 종목별 첫 매매일 앞 (MA60 예열 60 + 표시 35)봉부터만 싣는다(파일 크기).
    for code, rows in daily.items():
        first_day = min(d for c, d in cases if c == code)
        idx = next((i for i, b in enumerate(rows) if b["_date"] >= first_day), 0)
        daily[code] = rows[max(0, idx - 95):]
    data = {"trades": trades, "account": args.account, "trades_file": tpath.name,
            "daily": {c: [{k: b[k] for k in OHLCV} for b in rows] for c, rows in daily.items()},
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M")}
    html = (SKILL / "template.html").read_text(encoding="utf-8").replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"{len(trades)}건 → {out} ({out.stat().st_size / 1024:.0f}KB)")


if __name__ == "__main__":
    main()
