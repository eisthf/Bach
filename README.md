# Bach System

키움 API(`/home/rblue/work/kiwoom`)를 참고해 만든 종목별 차트·실시간 시세·
상태머신 기반 수동/자동매매 웹 앱.

- **백엔드**: Python FastAPI (`backend/`). 데이터/주문 제공자를 mock/kiwoom로 교체 가능.
- **프런트**: React + Vite + lightweight-charts (`frontend/`). Light theme.
- 기본 동작은 **mock 모드** — 자격증명 없이 UI·상태머신·차트·자동매매를 전부 시연.

## 실행

### 한 번에 (권장)
```bash
./dev.sh          # .env 설정 그대로 — 백엔드(8000) + 프런트(5173)
./dev.sh --demo   # 합성 mock 계좌 1개만. 장 시작/종료를 수동 토글
```
최초 1회 셋업(venv·npm install·`.env` 복사)도 자동으로 처리하며, Ctrl+C 한 번에
양쪽을 정리한다.

**`--demo`가 필요한 이유**: 장 시계는 전 계좌 공유라 kiwoom 계좌가 하나라도
있으면 실제 KST 시각으로 자동 판정되고, 수동 장 토글이 409로 거부된다
([아래](#장-시계는-전-계좌-공유--혼합-구성-시-수동-장-제어-불가) 참고). 주말이나
장 마감 시각에 UI·상태머신·자동매매를 시연하려면 `--demo`로 띄운다. 실거래
계좌를 배제하므로 실주문 위험이 없고, 종목 목록도 `state.demo.json`에 따로
저장해 평소 설정을 건드리지 않는다.

수동으로 따로 띄우려면:

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

### 원격 접속 (GCP VM에서 실행 중일 때)

앱은 WebSocket(`/ws`)으로 실시간 시세를 받고 Vite HMR도 WebSocket을 쓴다.
**인터넷 직결 환경(예: 집)에서는 외부 IP에 브라우저로 바로 접속해도 문제없다.**
(`vite.config.js`가 `server.host: true`로 모든 인터페이스에 바인딩하므로 `--host`
플래그를 따로 줄 필요는 없다. VM 공용 IP `34.64.x.x:5173` 형태로 접속 가능.)

다만 **HTTP 프록시를 경유**해 외부 IP에 직접 접속하면(예: 사내망, 또는 안드로이드
USB RNDIS + 폰의 HTTP 프록시 경유) 문제가 생긴다. HTTP 요청(GET 페이지/API)은
프록시가 중계하지만, **브라우저의 평문 `ws://`는 이 경로로 안정적으로 전달되지
않아** `/ws`(실시간 시세)가 끊기고 Vite HMR이 무한 새로고침 루프에 빠진다.
(정확한 원인은 환경마다 다를 수 있다 — 프록시가 `Upgrade` 헤더를 중계하지 않거나,
비표준 포트로의 `CONNECT`를 거부하는 등. 평문 `ws://`를 포워드 HTTP 프록시로
넘기는 것 자체가 원래 불안정한 시나리오다.)

이 경우 **SSH 포트 포워딩**으로 우회한다. SSH가 WebSocket을 평문 TCP로 감싸
프록시의 단일 `CONNECT` 터널을 통과시키므로, 중간 프록시는 WebSocket인지조차
모르고 정상 동작한다. (외부에 포트를 열지 않아 보안상으로도 낫다.)

```bash
# 로컬 PC에서. 프록시 뒤라면 ~/.ssh/config 의 Host 별칭을 써야
# ProxyCommand(프록시 경유)가 적용된다. IP를 직접 쓰면 config이 매칭되지
# 않아 ProxyCommand 없이 22번 직결을 시도하다 멈춘다.
ssh -L 5173:localhost:5173 -L 8000:localhost:8000 gcp-rblue
# 그 후 로컬 브라우저: http://localhost:5173
```

`~/.ssh/config` 예시:
```
Host gcp-rblue
  HostName 34.64.153.2
  User rblue
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  ProxyCommand /usr/bin/nc -X connect -x <proxy_host>:<proxy_port> %h %p
  ServerAliveInterval 5
  ServerAliveCountMax 10
  TCPKeepAlive yes
```

## 사용 흐름 (mock 데모)

1. 상단에서 **종목코드 추가** (예: `005930`). 카드(차트+패널)가 생성된다.
2. 차트: 3/5/10/30/60분봉 전환(기본 3분). 캔들은 테두리만(상승 적색/하락 청색),
   이평선 MA5(파랑)/MA10(분홍)/MA20(주황)/MA60(초록) 겹쳐 그림.
   차트에 마우스를 올리면 크로스헤어 수평선+가격 라벨(손절선 가늠용).
3. 우상단 **장 시작** 전에는 상태 버튼으로 `수동매매 ↔ 모니터` 토글. 실전 모드에서는
   장 종료 후나 주말에도 `다음 장 모니터 예약`이 가능하다.
4. **장 시작** 누르면 `모니터` 종목이 `자동매매`로 진입 → 상한가 따라잡기 엔진이
   틱에 반응해 분할매수/익절/손절 실행(로그 패널에 표시).
5. 자동매매 중엔 **PUSH**로 직접 인수하면 `수동매매`로 전환(장중 종착, 엔진 정리).
   별도 청산 버튼은 없다 — 청산은 매매의 *결과*이지 버튼이 아니다. 엔진이 전량
   매도해 보유수량이 0이 되면 자동으로 `수동매매`로 복귀하고, 사람이 직접 청산하려면
   PUSH로 인수한 뒤 수동매매에서 매도한다.
6. `수동매매`에서 매수금액(원)/매도수량(주) 입력 후 매수/매도.
7. **장 종료** 누르면 하루 사이클 종료 → 모든 종목이 `수동매매`로 복귀하고
   장 단계가 `장전`으로 리셋(엔진 정리). 다시 PUSH로 `모니터` 진입 가능.

## 상태머신

```
[*] --> MANUAL_TRADING
MANUAL_TRADING --> MONITOR : PUSH (장중이 아닐 때)
MONITOR --> MANUAL_TRADING : PUSH
MONITOR --> AUTO_TRADING : MARKET-OPEN
AUTO_TRADING --> MANUAL_TRADING : PUSH | POSITION-FLAT(보유수량→0)
(MONITOR|AUTO_TRADING) --> MANUAL_TRADING : MARKET-CLOSE
(장중 MANUAL_TRADING은 종착 — 나가는 전이 없음)
(POSITION-FLAT은 엔진 전량매도의 결과로 발생하는 이벤트 — 청산 버튼 없음)
(MARKET-CLOSE 시 장 단계도 장전(PRE_OPEN)으로 리셋 → 초기 상태 복귀)
```

## 상한가 종목 페이지

헤더의 **매매 / 상한가 종목** 탭으로 오간다(해시 라우팅 — 새로고침·뒤로가기 유지).

지정일 **D**의 종가가 **직전 거래일 D-1** 종가 대비 **+29%~+30%** 인 종목을
표(종목코드·종목명·시장·시가총액·종가·등락률)로 보여준다. 날짜를 비우고 조회하면
가장 최근 거래일을 서버가 잡는다. D-1은 달력상 전날이 아니라 직전 *거래일*로,
주말·공휴일은 시세가 비어 오는 것으로 판별해 건너뛴다.

> 29~30% 구간인 이유: 가격제한폭은 +30%지만 호가단위 반올림 때문에 상한가가
> 정확히 30%가 되는 경우는 드물다.

### 데이터 소스 — KRX Open API

키움 REST에는 **전종목 일별시세** 엔드포인트가 없다(종목별 조회뿐이라 날짜당
~2,800회 호출, 순위 API는 당일만 조회). 그래서 이 페이지만 KRX Open API를 쓴다 —
날짜당 시장별 1회 호출로 시가총액까지 함께 받는다.

```
# backend/.env
KRX_OPEN_API_KEY=<openapi.krx.co.kr 발급 인증키>
```
[openapi.krx.co.kr](https://openapi.krx.co.kr)에서 인증키를 발급받고 **'유가증권
일별매매정보'와 '코스닥 일별매매정보' 각각** 이용신청(승인 필요)을 마쳐야 한다.
승인 전이면 `401 Unauthorized Key`가 떨어지고 화면에 안내가 뜬다.

키가 없으면 **합성 mock 데이터**로 자동 대체돼 화면·흐름을 확인할 수 있고,
이때는 표 위에 노란 **데모 데이터** 배지가 붙는다. `SCREENER_SOURCE=krx|mock`로
소스를 강제할 수도 있다.

## 매매 전략

자동매매는 **상한가 따라잡기(ULC)** 전략을 쓴다 — 진입 필터, 시나리오별
분할매수, 익절/손절/트레일링, 평단·수량의 진실원(실체결 이벤트 + 계좌 보정)
등 상세는 [docs/trading-strategy.md](docs/trading-strategy.md) 참고.

## 실거래(키움) 모드 — multi-account

계좌는 `ACCOUNTS` 환경변수(쉼표구분)로 활성화하고, 계좌별 자격증명은
`<ID>_APPKEY` 식의 prefix 변수로 준다. `backend/.env`:
```
ACCOUNTS=mock,real

MOCK_PROVIDER=kiwoom      # kiwoom=키움 모의서버 | mock=합성 데이터(무자격증명)
MOCK_APPKEY=...
MOCK_SECRETKEY=...

REAL_PROVIDER=kiwoom      # ⚠️ 실전 — 실제 주문이 나간다
REAL_APPKEY=...
REAL_SECRETKEY=...
```
전체 옵션(CORS, API_TOKEN, 임의 계좌 추가 등)은 `backend/.env.example` 참고.
kiwoom 계좌인데 자격증명이 없으면 그 계좌만 건너뛰고, 활성 계좌가 없으면
합성 mock 계좌 하나로 대체돼 자격증명 없이도 데모가 돈다.

### 장 시계는 전 계좌 공유 — 혼합 구성 시 수동 장 제어 불가

장 단계(장전/장중/장종료)는 계좌별이 아니라 **전 계좌가 하나의 시계를
공유**한다. 두 계좌가 같은 한국 시장에서 거래하므로 장 시각도 하나여야
하기 때문이다. 이 시계의 모드는 활성 계좌 구성이 결정한다:

- **kiwoom 계좌가 하나라도 있으면**: 실제 KST 시계로 자동 판정
  (평일 09:00~15:30). 헤더의 수동 버튼(장 시작/종료/초기화)은 사라지고
  API로 호출해도 409로 거부된다 — 진실원이 실제 시각이기 때문.
- **합성 mock 계좌뿐이면**: 수동 토글로 장 이벤트를 데모할 수 있다.

따라서 `ACCOUNTS=mock,real`에서 mock을 합성(`MOCK_PROVIDER=mock`)으로
두더라도, real(kiwoom)이 함께 있으면 합성 계좌의 장 토글 데모는 불가하다.
수동 토글 데모가 필요하면 `ACCOUNTS=mock` + `MOCK_PROVIDER=mock`으로
kiwoom 계좌 없이 띄운다.

`app/providers/kiwoom_api.py`가 키움 REST/WebSocket을 **self-contained**로 구현한다
(외부 kiwoom 프로젝트 의존 없음). 토큰(au10001), 분봉(ka10080), 일봉(ka10081),
주문(kt10000/kt10001), 잔고(kt00018), 실시간 체결(WebSocket `0B`)을 직접 호출한다.
`app/providers/kiwoom.py`가 이를 DataProvider/Broker 계약에 맞춘다. 포지션은 계좌
잔고(kt00018)를 단일 진실원으로 삼는다. 자격증명/네트워크 준비 환경에서 검증 필요.

## 구조

```
backend/app/
  main.py            REST + WebSocket 엔드포인트
  hub.py             종목 허브(상태/틱태스크/자동매매/브로드캐스트)
  state_machine.py   상태머신 (장전/장중 가드)
  market_clock.py    장 단계
  models.py          Pydantic 모델
  screener.py        상한가 종목 스크리너 (+ 합성 mock 소스)
  providers/         mock | kiwoom 데이터·브로커, krx_api(전종목 일별시세)
  strategy/ulc.py    상한가 따라잡기 전략 엔진
frontend/src/
  store.jsx          전역 상태 + WebSocket
  router.js          해시 라우터 (매매 / 상한가 종목)
  pages/             UpperLimitPage(상한가 종목 표)
  components/        Chart, PriceTicker, StateButton, ManualTradePanel, AutoConfigForm,
                     StockInput, StockPanel, MarketControls, LogPanel, PageNav

dev.sh               개발 서버 일괄 실행 (--demo: 합성 mock 단독)
backend/tests/       pytest 회귀 테스트 (uv run pytest)

# frontend/smoke*.mjs : Playwright 헤드리스 스모크 테스트(개발용)
#   smoke.mjs 기본 흐름 | smoke_multi.mjs 다중계좌 | smoke-mobile.mjs 모바일
#   smoke-restore.mjs 재시작 복원 | smoke-screener.mjs 상한가 종목 페이지
```
