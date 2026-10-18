"""대금 양봉·구간 매매 실매매 기록 차트 생성기.

엔진 로그(logs/events.log 의 candle_trade 이벤트) + 계좌 체결 기록(state.{계좌}.trades.json)에서
매매 한 건(등록 → 엔진 매매 → 수동 인계 뒤 포지션이 0이 될 때까지)을 묶어, 키움 차트 API
(조회 전용, 주문 없음)로 일봉·3분봉을 받고 HTML 한 장으로 만든다.
봉 조회·캐시·HTML 조립은 ../_trade_chart_common 공통 코드.

- 매매 한 건: 종목의 '등록' 메시지부터 다음 '등록' 전까지. 체결이 없는 건(기한 만료·설정 중 삭제)은 뺀다.
  로그가 넘겨쓰기로 지워져 등록 기록이 없으면, 체결 기록의 주문 태그(strategy=candle/zone, ctx 의 등록 시각·
  기준 캔들·매입 구간·익절 %)로 다시 세운다. 사유도 태그 reason 을 먼저 쓴다.
- 체결 범위: 첫 엔진 매수 메시지 이후. 엔진 매수·매도 메시지 순서로 사유를 붙이고, 수동 인계 뒤
  체결은 '수동'으로 포함하되 보유 수량이 0이 되거나 그 종목의 상한가(ULC) 매매가 시작되면 멈춘다.
- 일봉: 기준 캔들(대금 양봉)·매입 구간·평단·익절선 표시. 3분봉: 체결이 있었던 날들(+ 첫날 전일 60봉 예열).

실행(backend 의 uv 환경에서):
  cd backend && uv run python ../.claude/skills/candle-trade-chart/candle_trade_chart.py
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SKILL = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL.parent / "_trade_chart_common"))
from common import (ROOT, Bars, chart_epoch, day_fills, fill_points, group_orders, iter_events,  # noqa: E402
                    load_fills, load_trade_events, ohlcv, regular, trim_daily, write_html, ymd)

WARMUP = 60  # 3분봉 MA60 예열


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


def num(s: str) -> float:
    return float(s.replace(",", ""))


def scan_log(account: str):
    """candle_trade 메시지 → 매매 건 목록, ULC 매매가 있었던 (code, day) 집합."""
    camps, ulc_days, cur = [], set(), {}
    for e in iter_events(account):
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
            if not c["handoff"]:
                c["handoff"], c["handoff_reason"] = t, body.split(":", 1)[-1].strip()
            c["end_reason"] = c["end_reason"] or body
        elif body.startswith("기한 만료") or body.startswith("1차 매수 실패"):
            c["end_reason"] = body
    return camps, ulc_days


def created_epoch(created: str) -> int | None:
    """CandleItem.created_at(KST ISO, 초 단위) → 차트 epoch."""
    try:
        return int(datetime.fromisoformat(created).replace(tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError):
        return None


def tag_campaigns(fills: list, camps: list) -> list:
    """로그에 등록 기록이 없는 매매 건을 체결 태그 맥락으로 다시 세운다(로그 건과 겹치면 뺀다)."""
    out = {}
    for f in sorted(fills, key=lambda f: f["time"]):
        if f.get("strategy") not in ("candle", "zone"):
            continue
        ctx = f.get("ctx") or {}
        reg = created_epoch(ctx.get("created", "")) or f["time"]
        key = (f["code"], ctx.get("created") or reg)
        if key in out:
            continue
        if any(c["code"] == f["code"] and abs(c["reg"] - reg) <= 60 for c in camps):
            continue  # 로그에 있는 건(등록 메시지 시각 ≈ created_at)
        base = None
        if ctx.get("base_date"):
            base = {"date": ctx["base_date"].replace("-", ""), "open": ctx.get("base_open"), "close": ctx.get("base_close")}
        out[key] = {"code": f["code"], "kind": f["strategy"], "reg": reg, "base": base, "zone": ctx.get("zone"),
                    "tp": ctx.get("take_profit_pct"), "entry_days": ctx.get("entry_days"), "buys": [], "sells": [],
                    "engine_from": f["time"], "handoff": None, "end_reason": None, "log": []}
    return list(out.values())


def short_reason(reason: str) -> str:
    """"트레일링 스톱: 고점 14,000 −5%" → "트레일링 스톱"."""
    return reason.split(":")[0].split(" (")[0]


def campaign_fills(c: dict, fills: list, ulc_days: set, next_reg: int | None,
                   days: list, bars, warn: list) -> list:
    """이 매매 건에 속한 체결(시간순). 거래일을 차례로 보며 Bach 체결 기록 + 계좌 주문체결내역
    (서버가 꺼져 있을 때 HTS로 낸 주문 등)을 모으고, 보유가 0이 되면 멈춘다."""
    if c["engine_from"] is None:
        return []
    start = c["engine_from"] - 120  # 매수 메시지가 체결 직전·직후로 찍힐 수 있다
    out, net = [], 0
    for d in days:
        if d < ymd(start) or (next_reg and d > ymd(next_reg)):
            continue
        if (c["code"], d) in ulc_days:
            break
        for f in day_fills(fills, c["code"], d, bars, warn):
            if f["time"] < start or (next_reg and f["time"] >= next_reg):
                continue
            out.append(f)
            net += f["qty"] if f["side"] == "buy" else -f["qty"]
            if net <= 0:
                return out
    return out


def main():
    args = parse_args()
    tpath, fills = load_fills(args.account)
    camps, ulc_days = scan_log(args.account)
    ulc_days |= {(f["code"], ymd(f["time"])) for f in fills if f.get("strategy") == "ulc"}
    camps = sorted(camps + tag_campaigns(fills, camps), key=lambda c: c["reg"])
    since = (args.since or "0000-00-00").replace("-", "")
    until = (args.until or "9999-99-99").replace("-", "")

    bars = Bars(args.account, args.refresh)
    warn: list = []
    daily_cache: dict = {}

    def first_day_of(c: dict) -> str:
        d = ymd(c["reg"])
        return min(d, c["base"]["date"]) if c["base"] else d

    picked = []
    for i, c in enumerate(camps):
        if not since <= ymd(c["reg"]) <= until or (args.code and c["code"] not in args.code):
            continue
        if args.kind and c["kind"] != args.kind:
            continue
        if c["engine_from"] is None:
            continue
        nxt = next((d["reg"] for d in camps[i + 1:] if d["code"] == c["code"]), None)
        key = (c["code"], first_day_of(c))
        if key not in daily_cache:
            daily_cache[key] = bars.daily(*key)
        days = [b["_date"] for b in daily_cache[key]]
        cf = campaign_fills(c, fills, ulc_days, nxt, days, bars, warn)
        if cf:
            picked.append((c, cf))
    if not picked:
        raise SystemExit("조건에 맞는 대금 양봉·구간 매매(체결 있는 건)가 없습니다.")

    # 로그에 인계 기록이 없으면(넘겨쓰기로 지워짐) 서버가 남긴 인계 기록(events.json)으로 채운다.
    # 같은 매매 건: 같은 종목 + 등록 시각(ctx.created) ≈ 건의 등록 시각, 없으면 건의 체결 기간 안.
    handoff_events = [e for e in load_trade_events(args.account)
                      if e.get("kind") == "handoff" and e.get("strategy") in ("candle", "zone")]
    for c, cf in picked:
        if c["handoff"]:
            continue
        for e in handoff_events:
            created = created_epoch((e.get("ctx") or {}).get("created", ""))
            same = (abs(created - c["reg"]) <= 60) if created else (cf[0]["time"] <= e["time"] <= cf[-1]["time"] + 86400 * 30)
            if e["code"] == c["code"] and same:
                c["handoff"], c["handoff_reason"] = e["time"], e.get("reason", "")
                break

    trades, daily_out = [], {}
    for c, cf in picked:
        code = c["code"]
        first_day = first_day_of(c)
        rows = daily_cache[(code, first_day)]

        # 사유: 주문 태그 reason 우선. 태그가 없으면 엔진 구간(인계 전) 주문은 엔진 메시지 순서, 그 뒤는 '수동'
        blabels, slabels = iter(c["buys"]), iter(c["sells"])
        orders = []
        for g in group_orders(cf):
            tag = g.pop("tag")
            engine = tag["strategy"] in ("candle", "zone") if tag else (c["handoff"] is None or g["time"] < c["handoff"])
            logged = next(blabels if g["side"] == "buy" else slabels, None) if engine else None
            if tag and engine and tag.get("reason"):
                label = short_reason(tag["reason"])
            elif tag and not engine and tag.get("reason"):
                label = tag["reason"]                    # 수동 매수·수동(영웅문S#) 등
            else:
                label = logged if engine and logged else "수동"
            if tag and engine:
                ctx = tag.get("ctx") or {}
                c["zone"] = c["zone"] or ctx.get("zone")
                c["tp"] = c["tp"] or ctx.get("take_profit_pct")
                c["entry_days"] = c["entry_days"] or ctx.get("entry_days")
            orders.append({**g, "label": label, "day": ymd(g["time"])})

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
        handoffs = []
        if c["handoff"]:
            # "사용자 수동 전환" → "사용자", "절반 익절 매도 실패 — 계좌에서 확인 필요" → 앞부분만
            label = (c.get("handoff_reason") or "수동 인계").replace(" 수동 전환", "").split(" — ")[0]
            handoffs.append({"time": c["handoff"], "label": label})
            notes.append(f"{datetime.fromtimestamp(c['handoff'], timezone.utc):%m-%d %H:%M} 수동 전환({label}) — "
                         f"이후 체결은 '수동', 차트의 옅은 배경")
        account_n = len({f["order_no"] for f in cf if f.get("source") == "account"})
        if account_n:
            notes.append(f"Bach 체결 기록에 없던 주문 {account_n}건을 계좌 주문체결내역에서 채움"
                         f"(서버가 꺼져 있을 때 HTS 등으로 낸 주문 — 시각은 주문 시각)")
        if hold > 0 and last:
            notes.append(f"{hold}주 보유 중 · 평가는 최근 일봉 종가 {last['close']:,.0f}({last['_date'][4:6]}/{last['_date'][6:]}) 기준")

        # 3분봉: 체결이 있었던 날들 + 첫날 직전 거래일 정규장 60봉(MA 예열)
        fill_days = sorted({o["day"] for o in orders})
        prev = bars.prev_day(rows, fill_days[0])
        m3 = regular(bars.min3(code, prev))[-WARMUP:] if prev else []
        day_starts = []
        for d in fill_days:
            day_starts.append({"day": d, "idx": len(m3)})
            m3 += regular(bars.min3(code, d))

        key = f"{code}_{len(trades)}"
        daily_out[key] = ohlcv(trim_daily(rows, first_day))
        trades.append({
            "code": code, "name": bars.name(code), "kind": c["kind"], "reg": ymd(c["reg"]),
            "base": c["base"], "zone": c["zone"], "tp": c["tp"], "entry_days": c["entry_days"],
            "end_reason": c["end_reason"], "orders": orders, "status": status,
            "buy_q": buy_q, "sell_q": sell_q, "buy_a": buy_a, "sell_a": sell_a, "hold": hold,
            "avg": avg, "pnl": pnl, "unreal": unreal, "notes": notes, "daily_key": key,
            "min3": ohlcv(m3), "day_starts": day_starts, "fills": fill_points(cf), "handoffs": handoffs,
            "log": [x for x in c["log"] if not x["text"].startswith("체결:")],
        })
        tag = "대금양봉" if c["kind"] == "candle" else "구간매매"
        extra = "" if unreal is None else f" 평가 {unreal:+,.0f}원"
        print(f"{ymd(c['reg'])} {code} {trades[-1]['name']} {tag} {status} 실현 {pnl:+,.0f}원{extra}")

    for w in warn:
        print(f"⚠ {w}")
    write_html(SKILL / "template.html", {
        "trades": trades, "daily": daily_out, "account": args.account, "trades_file": tpath.name,
    }, args.out)


if __name__ == "__main__":
    main()
