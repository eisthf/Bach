"""키움 연결 read-only 검증 (주문 전송 없음).

토큰 발급 → 분봉(ka10080) → 일봉 전일종가(ka10081) → 잔고(kt00018) 조회만 수행.
키는 backend/.env 가 있으면 그걸, 없으면 backend/.env.example 에서 읽는다.

실행: PROVIDER 무관. `python verify_kiwoom.py [종목코드]`
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).parent
# .env 우선, 없으면 .env.example
for fn in (".env", ".env.example"):
    p = HERE / fn
    if p.exists():
        load_dotenv(p)
        print(f"[env] loaded {fn}")
        break

from app.providers import kiwoom_api as kw  # noqa: E402

code = sys.argv[1] if len(sys.argv) > 1 else "005930"
appkey = os.getenv("APPKEY")
secretkey = os.getenv("SECRETKEY")
mock = (os.getenv("KIWOOM_MOCK", "true").lower() == "true")
print(f"[cfg] code={code} mock={mock} host={kw.rest_host(mock)}")

if not appkey or not secretkey:
    sys.exit("APPKEY/SECRETKEY 없음")

print("\n1) 토큰 발급…")
token = kw.get_access_token(appkey, secretkey, mock=mock)
print(f"   token: {token[:12]}… (len {len(token)})")

print("\n2) 3분봉 조회(ka10080)…")
bars = kw.fetch_min_bars(token, code, 3, mock=mock, lookback_extra=20)
print(f"   {len(bars)}개 봉")
if bars:
    b = bars[-1]
    print(f"   마지막 봉 date={b['_date']} O={b['open']:.0f} H={b['high']:.0f} "
          f"L={b['low']:.0f} C={b['close']:.0f} V={b['volume']:.0f}")

print("\n3) 전일 종가(ka10081)…")
print(f"   prev_close = {kw.fetch_prev_close(token, code, mock=mock)}")

print("\n4) 계좌 잔고(kt00018)…")
pos = kw.fetch_positions(token, mock=mock)
if not pos:
    print("   보유 종목 없음(또는 조회 결과 없음)")
for c, p in pos.items():
    print(f"   {c}: {p['quantity']}주 평단 {p['avg_price']:.0f} 현재 "
          f"{p['current_price']:.0f} 매도가능 {p['sellable_qty']}")

print("\n✅ read-only 검증 완료 (주문 전송 없음)")
