"""매매 차트 스킬 공통 코드(ulc-trade-chart, candle-trade-chart).

SKILL.md 가 없으니 스킬로 잡히지 않는다. 각 스킬 스크립트가 sys.path 에 이 폴더를 넣고 import 한다.

- Bars: 키움 차트 조회(ka10080 분봉·ka10081 일봉·종목명) + 캐시. 조회 전용, 주문 없음.
  캐시는 backend/backtest_data/trade_chart/ (gitignore) 한 곳 — 두 스킬이 같은 종목·같은 날을 공유한다.
- trades_path / load_fills: 계좌 체결 기록 파일
- group_orders: 체결을 주문번호로 묶기
- write_html: chart.css·chart.js 와 스킬 템플릿을 합쳐 데이터 JSON 을 넣은 HTML 한 장
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

COMMON = Path(__file__).resolve().parent
ROOT = COMMON.parents[2]
BACKEND = ROOT / "backend"
CACHE = BACKEND / "backtest_data" / "trade_chart"
OHLCV = ("time", "open", "high", "low", "close", "volume")
KST = 9 * 3600


def trades_path(account: str) -> Path:
    return BACKEND / ("state.trades.json" if account == "mock" else f"state.{account}.trades.json")


def load_fills(account: str) -> tuple[Path, list]:
    path = trades_path(account)
    if not path.exists():
        raise SystemExit(f"{path.name} 이 없습니다.")
    return path, json.loads(path.read_text(encoding="utf-8"))


def log_files(directory: Path | None = None) -> list[Path]:
    """이벤트 로그 파일을 오래된 것부터. 서버는 10MB마다 넘겨 쓴다(app/event_log.py RotatingFileHandler):
    events.log.10(가장 오래됨) … events.log.1, events.log(현재). 로그 위치는 서버와 같이 BACH_LOG_DIR 우선."""
    directory = directory or Path(os.getenv("BACH_LOG_DIR") or BACKEND / "logs")
    rotated = []
    for p in directory.glob("events.log.*"):
        suffix = p.name.rsplit(".", 1)[1]
        if suffix.isdigit():
            rotated.append((int(suffix), p))
    files = [p for _, p in sorted(rotated, reverse=True)]
    current = directory / "events.log"
    return files + [current] if current.exists() else files


def load_trade_events(account: str) -> list:
    """서버가 체결 기록 옆에 남긴 매매 이벤트(state.{계좌}.events.json, 예: kind="handoff" 인계).
    이벤트 로그와 달리 지워지지 않는다. 2026-10-18 서버 변경 전 이벤트는 없다."""
    name = "state.events.json" if account == "mock" else f"state.{account}.events.json"
    path = BACKEND / name
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def iter_events(account: str):
    """이벤트 로그(넘겨 쓴 파일 포함) 중 그 계좌 이벤트(dict)를 시간순으로."""
    files = log_files()
    if not files:
        raise SystemExit("이벤트 로그(events.log)가 없습니다.")
    for path in files:
        for line in open(path, encoding="utf-8"):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("account") == account:
                yield e


def chart_epoch(ts: str) -> int:
    """로그 UTC 타임스탬프 → 차트 epoch(KST 벽시계를 UTC로 둔 값, 체결 time 과 같은 축)."""
    return int(datetime.fromisoformat(ts).timestamp()) + KST


def ymd(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y%m%d")


def ohlcv(rows: list) -> list:
    return [{k: b[k] for k in OHLCV} for b in rows]


def regular(rows: list) -> list:
    """정규장 분봉만(09:00~15:27 시작 봉)."""
    return [b for b in rows if "0900" <= b["_hhmm"] <= "1527"]


def order_key(f: dict) -> str:
    """체결의 주문 식별자: order_no 필드(태그 도입 뒤) 또는 키움 id 두 번째 칸."""
    if f.get("order_no"):
        return f["order_no"]
    parts = f["id"].split(":")
    return parts[1] if len(parts) == 3 else f["id"]


def group_orders(fills: list) -> list:
    """체결을 주문번호로 묶어 시간순 [{side, qty, amt, time, price, tag}].

    tag: 서버가 주문을 낼 때 붙인 {strategy, reason, ctx}(app/trade_history.py tag_order). 예전 체결엔 없다(None).
    """
    groups = {}
    for f in fills:
        g = groups.setdefault(order_key(f), {"side": f["side"], "qty": 0, "amt": 0.0, "time": f["time"], "tag": None})
        g["qty"] += f["qty"]
        g["amt"] += f["qty"] * f["price"]
        g["time"] = min(g["time"], f["time"])
        if g["tag"] is None and f.get("strategy"):
            g["tag"] = {k: f.get(k) for k in ("strategy", "reason", "ctx")}
    out = sorted(groups.values(), key=lambda g: g["time"])
    for g in out:
        g["price"] = round(g["amt"] / g["qty"], 1)
    return out


def fill_points(fills: list) -> list:
    return [{k: f[k] for k in ("time", "side", "qty", "price")} for f in fills]


class Bars:
    """키움 차트 조회(캐시 우선). 토큰은 캐시 미스가 처음 날 때만 발급."""

    def __init__(self, account: str, refresh: bool = False):
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

    def min3(self, code: str, day: str) -> list:
        """그날 3분봉(08:00~20:00 통합 포함 원본). 정규장만 쓰려면 regular()."""
        return self._cached(f"{code}_{day}_3m.json", lambda: self.kw.fetch_min_bars_on(
            self.token(), code, day, tic_scope=3, mock=self.cfg.kiwoom_mock))

    def daily(self, code: str, since: str) -> list:
        """오늘까지 일봉. ``since``(YYYYMMDD) 앞 MA60 예열 + 여유 100봉이 들어가게 조회한다.
        거래량 0 봉(장 시작 전 오늘 자리)은 뺀다."""
        asof = datetime.now().strftime("%Y%m%d")
        days = (datetime.now() - datetime.strptime(since, "%Y%m%d")).days
        lookback = int(days * 0.7) + 100  # 달력일 → 거래일 대략 0.7배
        rows = self._cached(f"{code}_daily_{asof}_{lookback}.json", lambda: self.kw.fetch_day_bars(
            self.token(), code, mock=self.cfg.kiwoom_mock, today=asof, lookback_extra=lookback))
        return [b for b in rows if b.get("volume", 0) > 0]

    def name(self, code: str) -> str:
        return self._cached(f"{code}_name.json", lambda: self.kw.fetch_stock_name(
            self.token(), code, mock=self.cfg.kiwoom_mock)) or code

    def prev_day(self, daily: list, day: str) -> str | None:
        prev = [b["_date"] for b in daily if b["_date"] < day]
        return prev[-1] if prev else None


def trim_daily(rows: list, first_day: str, before: int = 95) -> list:
    """일봉을 first_day 앞 ``before``봉(MA60 예열 60 + 표시 35)부터만 — HTML 크기를 줄인다."""
    idx = next((i for i, b in enumerate(rows) if b["_date"] >= first_day), len(rows))
    return rows[max(0, idx - before):]


def write_html(template: Path, data: dict, out: str | Path) -> Path:
    """스킬 템플릿의 /*__CHART_CSS__*/·/*__CHART_JS__*/·__DATA__ 를 채워 저장."""
    html = template.read_text(encoding="utf-8")
    for mark, name in (("/*__CHART_CSS__*/", "chart.css"), ("/*__CHART_JS__*/", "chart.js")):
        if mark not in html:
            raise SystemExit(f"{template} 에 {mark} 자리가 없습니다.")
        html = html.replace(mark, (COMMON / name).read_text(encoding="utf-8"))
    data = {**data, "generated": datetime.now().strftime("%Y-%m-%d %H:%M")}
    html = html.replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"{len(data.get('trades', []))}건 → {out} ({out.stat().st_size / 1024:.0f}KB)")
    return out
