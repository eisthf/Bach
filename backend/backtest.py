"""ULC 자동매매 백테스트 CLI.

과거 날짜의 1분봉을 실제 전략 엔진에 재생해 그날 자동매매했다면 어땠을지 본다.
근사 가정은 ``app/backtest.py`` 머리말 참고. 조회는 차트 API(읽기 전용)만 쓰며
주문은 절대 내지 않는다. 받은 데이터는 ``backtest_data/`` 에 캐시한다.

예:
  uv run python backtest.py 079650:20260903
  uv run python backtest.py 079650:20260903 001520:20260921 --set max_buy_amount=1000000
  uv run python backtest.py 079650:20260903 --state real          # 실계좌 저장 설정 사용
  uv run python backtest.py 079650:20260903 --state real \\
      --variant "3분봉:use_3min_bar_timing=true" --variant "1차만:ulc_first_buy_only=true"
  uv run python backtest.py 079650:20260903 --order all -v
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
sys.path.insert(0, str(HERE))

from app.backtest import ORDERS, COMMISSION, SELL_TAX, load_day, parse_case, replay  # noqa: E402
from app.models import AutoConfig  # noqa: E402

CACHE_DIR = HERE / "backtest_data"


def parse_overrides(items: list[str]) -> dict:
    """['k=v', ...] → AutoConfig 필드 dict. 모르는 키는 오류."""
    out = {}
    for item in items:
        key, sep, value = item.partition("=")
        key = key.strip()
        if not sep or key not in AutoConfig.model_fields:
            known = ", ".join(AutoConfig.model_fields)
            raise SystemExit(f"설정 오류: {item!r}. 사용 가능한 키: {known}")
        out[key] = value.strip()
    return out


def state_config(account: str, code: str) -> dict:
    """state.<account>.json 에 저장된 종목 설정. 그 종목이 없으면 첫 종목 설정."""
    name = "state.json" if account == "mock" else f"state.{account}.json"
    path = HERE / name
    if not path.exists():
        raise SystemExit(f"{name} 이 없습니다.")
    stocks = json.loads(path.read_text(encoding="utf-8")).get("stocks") or []
    if not stocks:
        raise SystemExit(f"{name} 에 저장된 종목이 없습니다.")
    chosen = next((s for s in stocks if s.get("code") == code), stocks[0])
    return {k: v for k, v in (chosen.get("config") or {}).items() if k in AutoConfig.model_fields}


def token_factory(account: str):
    """필요할 때 한 번만 토큰을 발급한다(전부 캐시 적중이면 발급하지 않음)."""
    from app.accounts import _account_config
    from app.providers import kiwoom_api as kw

    cfg = _account_config(account)
    if cfg.provider != "kiwoom" or not cfg.appkey or not cfg.secret:
        raise SystemExit(
            f"계좌 '{account}' 에 키움 자격증명이 없습니다. "
            f"--account 로 키움 계좌를 지정하거나 캐시된 데이터만 사용하세요.")
    holder = {}

    def get() -> str:
        if "token" not in holder:
            holder["token"] = kw.fetch_access_token(cfg.appkey, cfg.secret, mock=cfg.kiwoom_mock).value
        return holder["token"]
    return get, cfg.kiwoom_mock


def won(value: float) -> str:
    return f"{value:+,.0f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cases", nargs="+", help="종목코드:날짜 (예: 079650:20260903)")
    ap.add_argument("--account", default="real", help="조회에 쓸 키움 계좌 prefix (기본 real)")
    ap.add_argument("--state", metavar="ACCOUNT", help="그 계좌의 저장된 자동매매 설정을 기본값으로 사용")
    ap.add_argument("--set", dest="sets", action="append", default=[], metavar="KEY=VALUE",
                    help="AutoConfig 필드 덮어쓰기 (반복 가능)")
    ap.add_argument("--variant", action="append", default=[], metavar="NAME:K=V[,K=V]",
                    help="비교할 설정 변형 (반복 가능). 기준 설정 위에 덮어쓴다")
    ap.add_argument("--order", default="auto", choices=[*ORDERS, "all"],
                    help="1분봉 안 고가/저가 순서 가정 (기본 auto)")
    ap.add_argument("--commission", type=float, default=COMMISSION, help="수수료율(매수·매도 각각)")
    ap.add_argument("--tax", type=float, default=SELL_TAX, help="매도 거래세율")
    ap.add_argument("--refresh", action="store_true", help="캐시 무시하고 다시 조회")
    ap.add_argument("-v", "--verbose", action="store_true", help="엔진 로그 출력")
    args = ap.parse_args()

    cases = [parse_case(c) for c in args.cases]
    base = parse_overrides(args.sets)
    variants = [("기준", {})]
    for spec in args.variant:
        name, _, body = spec.partition(":")
        variants.append((name.strip() or spec, parse_overrides([x for x in body.split(",") if x.strip()])))
    orders = list(ORDERS) if args.order == "all" else [args.order]

    token_fn = mock = None
    totals: dict[str, list] = {}
    for code, date in cases:
        if token_fn is None:
            cached = (CACHE_DIR / f"{code}_{date}.json").exists() and not args.refresh
            if not cached:
                token_fn, mock = token_factory(args.account)
        day = load_day(code, date, token_fn=token_fn or (lambda: ""), mock=bool(mock),
                       cache_dir=CACHE_DIR, refresh=args.refresh)
        gap = (day.z / day.x - 1) * 100
        lows = min(b["low"] for b in day.bars)
        highs = max(b["high"] for b in day.bars)
        print(f"\n■ {code} {date[:4]}-{date[4:6]}-{date[6:]}  X(전일종가) {day.x:,.0f} · "
              f"Z(시가) {day.z:,.0f} ({gap:+.2f}%) · 저 {lows:,.0f} · 고 {highs:,.0f} · 종가 {day.close:,.0f}")
        base_cfg = {**(state_config(args.state, code) if args.state else {}), **base}
        for name, overrides in variants:
            config = AutoConfig(**{**base_cfg, **overrides})
            for order in orders:
                r = asyncio.run(replay(day, config, order, args.commission, args.tax))
                label = name + (f" [{order}]" if len(orders) > 1 else "")
                if not r.trades:
                    reason = next((l for l in r.logs if "SKIP" in l), "매매 없음")
                    print(f"  {label}: 매매 없음 — {reason}")
                else:
                    held = f" · 미청산 {r.held}주 종가평가" if r.held else ""
                    print(f"  {label}: 순손익 {won(r.net_pnl)}원 ({r.return_pct:+.2f}%) · "
                          f"세전 {won(r.pnl)}원 · 투입 {r.cost:,.0f}원{held}")
                    for t in r.trades:
                        side = "매수" if t.side == "buy" else "매도"
                        print(f"      {t.hhmm[:2]}:{t.hhmm[2:]} {side} {t.qty:>5}주 @ {t.price:,.0f}")
                if args.verbose:
                    for line in r.logs:
                        print(f"      · {line}")
                totals.setdefault(label, []).append(r)

    if len(cases) > 1:
        print("\n■ 합계")
        for label, results in totals.items():
            traded = [r for r in results if r.trades]
            wins = sum(1 for r in traded if r.net_pnl > 0)
            print(f"  {label}: 순손익 {won(sum(r.net_pnl for r in results))}원 · "
                  f"매매 {len(traded)}/{len(results)}일 · 승 {wins} 패 {len(traded) - wins}")


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
