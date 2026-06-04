# Bach — 주식 거래 웹 앱

키움 API(`/home/rblue/work/kiwoom`)를 참고해 만든 종목별 차트·실시간 시세·
상태머신 기반 수동/자동매매 웹 앱.

- **백엔드**: Python FastAPI (`backend/`). 데이터/주문 제공자를 mock/kiwoom로 교체 가능.
- **프런트**: React + Vite + lightweight-charts (`frontend/`). Light theme.
- 기본 동작은 **mock 모드** — 자격증명 없이 UI·상태머신·차트·자동매매를 전부 시연.

## 실행

### 1) 백엔드 (포트 8000)
```bash
cd backend
uv venv --python 3.11
uv pip install -e .
cp .env.example .env          # 기본 PROVIDER=mock
uv run uvicorn app.main:app --reload
```

### 2) 프런트 (포트 5173)
```bash
cd frontend
npm install
npm run dev
# http://localhost:5173
```
Vite가 `/api`·`/ws`를 백엔드(8000)로 프록시한다.

## 사용 흐름 (mock 데모)

1. 상단에서 **종목코드 추가** (예: `005930`). 카드(차트+패널)가 생성된다.
2. 차트: 3/5/10/30/60분봉 전환(기본 3분). 캔들은 테두리만(상승 적색/하락 청색),
   이평선 MA5(파랑)/MA10(분홍)/MA20(주황)/MA60(초록) 겹쳐 그림.
   차트에 마우스를 올리면 크로스헤어 수평선+가격 라벨(손절선 가늠용).
3. 우상단 **장 시작** 전(장전)에는 상태버튼 PUSH로 `수동매매 ↔ 모니터` 토글.
4. **장 시작** 누르면 `모니터` 종목이 `자동매매`로 진입 → 상한가 따라잡기 엔진이
   틱에 반응해 분할매수/익절/손절 실행(로그 패널에 표시).
5. 자동매매 중 **PUSH** 또는 **청산(LIQUIDATE)** → `수동매매`로 전환(장중 종착).
6. `수동매매`에서 매수금액(원)/매도수량(주) 입력 후 매수/매도.

## 상태머신

```
[*] --> MANUAL_TRADING
MANUAL_TRADING --> MONITOR : PUSH (장전에만)
MONITOR --> MANUAL_TRADING : PUSH
MONITOR --> AUTO_TRADING : MARKET-OPEN
AUTO_TRADING --> MANUAL_TRADING : PUSH | LIQUIDATE
(장중 MANUAL_TRADING은 종착 — 나가는 전이 없음)
```

## 실거래(키움) 모드

`backend/.env`:
```
PROVIDER=kiwoom
APPKEY=...
SECRETKEY=...
KIWOOM_MOCK=true          # 모의투자 서버
KIWOOM_PATH=/home/rblue/work/kiwoom
```
`app/providers/kiwoom.py`가 kiwoom 프로젝트의 `get_access_token`,
`place_buy_order`/`place_sell_order`, 분봉(ka10080), 실시간 틱(WebSocket `0B`)을
재사용한다. 자격증명/네트워크가 준비된 환경에서 검증 필요.

## 구조

```
backend/app/
  main.py            REST + WebSocket 엔드포인트
  hub.py             종목 허브(상태/틱태스크/자동매매/브로드캐스트)
  state_machine.py   상태머신 (장전/장중 가드)
  market_clock.py    장 단계
  models.py          Pydantic 모델
  providers/         mock | kiwoom 데이터·브로커
  strategy/ulc.py    상한가 따라잡기 전략 엔진
frontend/src/
  store.jsx          전역 상태 + WebSocket
  components/        Chart, PriceTicker, StateButton, ManualTradePanel,
                     AutoConfigForm, StockInput, StockPanel, MarketControls, LogPanel

# frontend/smoke.mjs : Playwright 헤드리스 스모크 테스트(개발용)
```
