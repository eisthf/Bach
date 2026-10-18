"""상한가 따라잡기(ULC) 실매매 기록 차트 생성기.

계좌 체결 기록(state.{계좌}.trades.json) + 엔진 로그(logs/events.log)에서 ULC 매매(종목·날짜)를
찾아, 키움 차트 API(조회 전용, 주문 없음)로 3분봉·일봉을 받고 HTML 한 장으로 만든다.
봉 조회·캐시·HTML 조립은 ../_trade_chart_common 공통 코드.

- 체결 기록의 주문 태그(서버가 주문을 낼 때 붙인 strategy·reason·ctx)를 먼저 쓰고, 태그가 없는
  예전 체결은 엔진 로그로 판정한다.
- ULC 매매 판정: 그날 그 종목에 strategy="ulc" 체결이 있거나, '자동매매 시작 (X=…, Z=…)' 엔진
  메시지와 체결이 있으면. 대금 양봉·구간 매매와 순수 수동매매는 자동 제외된다.
- 체결은 주문번호로 묶고 사유는 태그 reason, 없으면 엔진 메시지 순서(1차/2차/3차 매수,
  익절/트레일링/손절)로 붙인다. 수동 태그 주문이나 메시지보다 많은 주문은 '수동'.
- 3분봉 MA60 예열용으로 직전 거래일 정규장 3분봉 마지막 60개를 앞에 붙인다.

실행(backend 의 uv 환경에서):
  cd backend && uv run python ../.claude/skills/ulc-trade-chart/ulc_trade_chart.py
  ... --since 2026-10-01 --until 2026-10-15 --account real --out ../research/ulc_trades/index.html
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SKILL = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL.parent / "_trade_chart_common"))
from common import (ROOT, Bars, chart_epoch, day_fills, fill_points, group_orders, iter_events,  # noqa: E402
                    load_fills, load_trade_events, net_qty, ohlcv, order_key, regular, trim_daily,
                    write_html, ymd)

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


def scan_log(account: str) -> dict:
    """(code, YYYYMMDD) → 엔진 메시지 요약."""
    msgs = defaultdict(lambda: {"buy": [], "sell": [], "sc": None, "x": None, "z": None, "handoffs": [], "exit": None})
    for e in iter_events(account):
        if (e.get("event") == "state_transition" and e.get("from_state") == "AUTO_TRADING"
                and e.get("to_state") in HANDOFF_STATES and e.get("code")):
            t = chart_epoch(e["timestamp"])
            msgs[(e["code"], ymd(t))]["handoffs"].append({"time": t, "reason": e.get("reason", "")})
            continue
        if e.get("event") != "message":
            continue
        m = re.match(r"\[(\w+)\] (.*)", e.get("text", ""), re.S)
        if not m:
            continue
        t = chart_epoch(e["timestamp"])
        d = msgs[(m.group(1), ymd(t))]
        body = m.group(2)
        if mm := re.match(r"SC(\d) 진입", body):
            d["sc"] = int(mm.group(1))
        elif mm := re.match(r"자동매매 시작 \(X=([\d,]+), Z=([\d,]+)\)", body):
            d["x"], d["z"] = (float(v.replace(",", "")) for v in mm.groups())
            if e.get("config"):   # 2026-10-18 이후 로그: 그 종목의 설정 전체
                d["exit"] = e["config"]
        elif mm := re.match(r"(\d)차 매수", body):
            d["buy"].append(f"{mm.group(1)}차")
        elif body.startswith("익절"):
            d["sell"].append("익절")
        elif body.startswith("트레일링"):
            d["sell"].append("트레일링")
        elif body.startswith("손절:"):
            d["sell"].append("손절")
    return msgs


# AUTO_TRADING에서 나간 전환의 도착 상태. MANUAL_TRADING은 2026-10-18 이전 로그, 이후엔
# IDLE(그날 자동매매 종료) / MANUAL(수동 매매 목록으로 옮김).
HANDOFF_STATES = ("MANUAL_TRADING", "IDLE", "MANUAL")

# AUTO_TRADING → 수동 전환 원인(hub.py) → 차트 라벨
HANDOFF_LABELS = {
    "PUSH": "PUSH로 중단", "STOP_MANUAL_HANDOFF": "손절 기준 도달 인계", "ENGINE_DONE": "엔진 종료 뒤 잔량",
    "SERVER_RESTORE": "재시작 인계", "AUTO_SETUP_TIMEOUT": "준비 시간 초과", "MARKET_CLOSE": "장 마감",
    "MARKET_RESET": "장 초기화", "USER_MANUAL": "수동 전환", "AUTO_CARRY_TIMEOUT": "이월 재개 실패",
}


def handoffs_with_position(transitions: list, fills: list) -> list:
    """보유 수량이 남은 채 자동매매가 끝난 전환만(= 사람이 넘겨받은 시점).

    ENGINE_DONE 은 보통 전량 청산 뒤라 수량이 0이다. 마지막 매도 체결 시각이 전환 로그보다 조금
    늦게 찍힐 수 있어 ENGINE_DONE 만 60초 뒤 체결까지 보고 판단한다."""
    out = []
    for h in transitions:
        until = h["time"] + (60 if h["reason"] == "ENGINE_DONE" else 0)
        net = sum(f["qty"] if f["side"] == "buy" else -f["qty"] for f in fills if f["time"] <= until)
        if net > 0:
            out.append({"time": h["time"], "label": HANDOFF_LABELS.get(h["reason"], h["reason"]), "qty": net})
    return out


def md(day: str) -> str:
    return f"{day[4:6]}/{day[6:]}"


def ulc_label(reason: str) -> str:
    """태그 reason → 차트 라벨. "익절 절반"→익절, "트레일링 스탑 (max …)"→트레일링, "트레일링 중 손절"→손절."""
    if "손절" in reason:
        return "손절"
    if reason.startswith("트레일링"):
        return "트레일링"
    if reason.startswith("익절"):
        return "익절"
    return reason


def main():
    args = parse_args()
    tpath, fills = load_fills(args.account)
    msgs = scan_log(args.account)
    # 서버가 남긴 인계 기록(지워지지 않음)을 로그의 상태 전환과 합친다. 같은 인계면(±5초·같은 원인) 하나로.
    for e in load_trade_events(args.account):
        if e.get("kind") != "handoff" or e.get("strategy") != "ulc":
            continue
        hs = msgs[(e["code"], ymd(e["time"]))]["handoffs"]
        if not any(abs(h["time"] - e["time"]) <= 5 and h["reason"] == e.get("reason") for h in hs):
            hs.append({"time": e["time"], "reason": e.get("reason", "")})
            hs.sort(key=lambda h: h["time"])
    since = (args.since or "0000-00-00").replace("-", "")
    until = (args.until or "9999-99-99").replace("-", "")

    fill_days = sorted({(f["code"], ymd(f["time"])) for f in fills}, key=lambda c: (c[1], c[0]))
    ulc_tagged = {(f["code"], ymd(f["time"])) for f in fills if f.get("strategy") == "ulc"}
    cases = [(c, d) for c, d in fill_days
             if ((c, d) in ulc_tagged or msgs.get((c, d), {}).get("x") is not None)
             and since <= d <= until and (not args.code or c in args.code)]
    if not cases:
        raise SystemExit("조건에 맞는 상한가(ULC) 매매가 없습니다.")

    bars = Bars(args.account, args.refresh)
    from app.models import AutoConfig                          # Bars 가 backend 를 경로에 넣은 뒤
    from app.strategy.ulc import EXIT_KEYS, describe_exit

    def exit_settings_of(cfg: dict) -> dict:
        """로그·태그의 설정(전체 또는 청산 키만) → 청산 설정. 빠진 키는 기본값."""
        base = AutoConfig().model_dump()
        return {k: cfg.get(k, base[k]) for k in EXIT_KEYS}
    codes = sorted({c for c, _ in cases})
    first_of = {c: min(d for cc, d in cases if cc == c) for c in codes}
    daily = {c: bars.daily(c, first_of[c]) for c in codes}

    warn: list = []
    case_set = set(cases)
    trades = []
    for code, day in cases:
        # 그날 체결(Bach 기록 + 계좌 주문체결내역). 그날 청산되지 않았으면 다음 거래일들을 따라가며
        # 보유가 0이 될 때까지 묶는다(그 종목의 다음 ULC 매매일 전까지). 서버가 꺼져 있을 때
        # HTS로 판 경우(2026-10-06 KBI동양철관)도 계좌 내역으로 잡힌다.
        all_fills = day_fills(fills, code, day, bars, warn)
        days_used = [day]
        for d in (b["_date"] for b in daily[code] if b["_date"] > day):
            if net_qty(all_fills) <= 0 or (code, d) in case_set:
                break
            more = day_fills(fills, code, d, bars, warn)
            if more:
                all_fills += more
                days_used.append(d)
        info = msgs[(code, day)]
        blabels, slabels = iter(info["buy"]), iter(info["sell"])
        orders = []
        for g in group_orders(all_fills):
            tag = g.pop("tag")
            if tag and tag["strategy"] != "ulc":
                label = tag.get("reason") or "수동"
            else:
                # 엔진 주문은 로그 메시지와 같은 순서라 태그가 있어도 로그 차례를 넘긴다.
                logged = next(blabels if g["side"] == "buy" else slabels, None)
                label = ulc_label(tag["reason"]) if tag and tag.get("reason") else (logged or "수동")
            if tag and tag["strategy"] == "ulc" and tag.get("ctx"):
                ctx = tag["ctx"]
                info["sc"] = info["sc"] or ctx.get("scenario")
                info["x"] = info["x"] if info["x"] is not None else ctx.get("x")
                info["z"] = info["z"] if info["z"] is not None else ctx.get("z")
                info["exit"] = ctx.get("exit") or info["exit"]
            orders.append({**g, "label": label})
        side_sum = lambda side, k: sum(o[k] for o in orders if o["side"] == side)
        buy_q, sell_q = side_sum("buy", "qty"), side_sum("sell", "qty")
        buy_a, sell_a = side_sum("buy", "amt"), side_sum("sell", "amt")
        closed = buy_q == sell_q and buy_q > 0
        labels = [o["label"] for o in orders if o["side"] == "sell"]
        if not closed:
            result = "미청산"
        elif "손절" in labels:
            result = "손절"
        elif "트레일링" in labels:
            result = "익절+트레일링"
        elif labels and all(lab.startswith("수동") for lab in labels):
            result = "수동 청산"
        else:
            result = "청산"
        handoffs = handoffs_with_position(info["handoffs"], all_fills)
        notes = [f"{datetime.fromtimestamp(h['time'], timezone.utc):%H:%M} 수동 전환({h['label']}) — "
                 f"그때 보유 {h['qty']}주, 이후는 사람 관리(차트의 옅은 배경)" for h in handoffs]
        if len(days_used) > 1:
            notes.append(f"그날 청산되지 않아 {', '.join(md(d) for d in days_used[1:])}의 체결까지 이어서 표시")
        account_n = len({order_key(f) for f in all_fills if f.get("source") == "account"})
        if account_n:
            notes.append(f"Bach 체결 기록에 없던 주문 {account_n}건을 계좌 주문체결내역에서 채움"
                         f"(서버가 꺼져 있을 때 HTS 등으로 낸 주문 — 시각은 주문 시각)")
        if not closed:
            notes.append(f"{buy_q - sell_q}주 보유 중(마지막 확인 {md(days_used[-1])}) — 손익 미산정")
        prev = bars.prev_day(daily[code], day)
        m3 = regular(bars.min3(code, prev))[-WARMUP:] if prev else []
        day_starts = []
        for d in days_used:
            day_starts.append({"day": d, "idx": len(m3)})
            m3 += regular(bars.min3(code, d))
        trades.append({
            "code": code, "name": bars.name(code), "day": day,
            "sc": info["sc"], "x": info["x"], "z": info["z"],
            "exit": describe_exit(exit_settings_of(info["exit"])) if info["exit"] else None,
            "orders": orders, "buy_q": buy_q, "sell_q": sell_q, "buy_a": buy_a, "sell_a": sell_a,
            "pnl": (sell_a - buy_a) if closed else None, "result": result, "notes": notes,
            "min3": ohlcv(m3), "prev_n": day_starts[0]["idx"], "day_starts": day_starts,
            "fills": fill_points(all_fills),
            "handoffs": [{"time": h["time"], "label": h["label"]} for h in handoffs],
        })
        pnl = trades[-1]["pnl"]
        extra = f" (+{len(days_used) - 1}일)" if len(days_used) > 1 else ""
        print(f"{day} {code} {trades[-1]['name']} SC{info['sc']} {result}{extra} "
              f"{'' if pnl is None else f'{pnl:+,.0f}원'}")
    for w in warn:
        print(f"⚠ {w}")

    write_html(SKILL / "template.html", {
        "trades": trades, "account": args.account, "trades_file": tpath.name,
        "daily": {c: ohlcv(trim_daily(rows, first_of[c])) for c, rows in daily.items()},
    }, args.out)


if __name__ == "__main__":
    main()
