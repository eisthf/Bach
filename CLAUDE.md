# CLAUDE.md

이 파일은 이 저장소에서 작업하는 Claude Code에게 지침을 제공한다.

## 프로젝트 개요

**Bach** — 종목별 차트·실시간 시세·상태머신 기반 수동/자동매매 웹 앱.
키움증권 REST/WebSocket API를 붙여 실거래도 하지만, 기본은 **mock 모드**라
자격증명 없이 UI·상태머신·차트·자동매매를 전부 시연할 수 있다.

- **백엔드**: Python 3.11 + FastAPI (`backend/`). REST + WebSocket 단일 프로세스.
- **프런트**: React 18 + Vite + lightweight-charts (`frontend/`). Light theme.
- **multi-account**: 여러 계좌(예: 모의투자 + 실전)를 한 화면에서 동시 운용.

상세 사양은 [project_requirement.md](project_requirement.md), 사용 흐름·원격 접속·
실거래 설정은 [README.md](README.md) 참고.

## 개발 명령

```bash
./dev.sh          # 백엔드(8000) + 프런트(5173) 동시 실행. .env 설정 그대로
./dev.sh --demo   # 합성 mock 계좌 1개만. 장 시작/종료 수동 토글 가능 (실주문 없음)
```
`dev.sh`가 최초 1회 셋업(venv·`uv pip install`·`npm install`·`.env` 복사)도 처리한다.
Ctrl+C 한 번에 양쪽 정리. Vite가 `/api`·`/ws`를 백엔드로 프록시한다.

```bash
cd backend && uv run pytest        # 백엔드 회귀 테스트 (pytest, asyncio_mode=auto)
```

프런트 스모크(Playwright, 개발용, 서버가 떠 있어야 함):
```bash
cd frontend && node smoke.mjs          # 기본 흐름
node smoke_multi.mjs                    # multi-account
node smoke-mobile.mjs                   # 모바일 레이아웃
node smoke-restore.mjs                  # 재시작 후 상태 복원
node smoke-screener.mjs                 # 상한가 종목 페이지
```
포트 8000이 이미 쓰이고 있으면 `BACH_BACKEND_PORT`로 두 번째 인스턴스를 띄운다
(`BACH_BACKEND_PORT=8010 npx vite --port 5174`).

`backend/verify_kiwoom.py` — 실제 키움 자격증명/네트워크 환경에서 provider 검증용.

## 아키텍처

### 백엔드 (`backend/app/`)

| 파일 | 역할 |
|---|---|
| `main.py` | FastAPI 진입점. REST + `/ws` WebSocket. 계좌 스코프 라우팅(`/api/{account}/...`), 전역 장 시계(`/api/market*`), 선택적 `X-API-Token` 인증 |
| `hub.py` | `AccountManager`(여러 계좌 총괄, 공유 장 시계 + 공유 WS 버스) + 계좌별 `Hub`(종목·상태머신·틱태스크·자동매매 엔진·미체결·영속화) |
| `accounts.py` | `ACCOUNTS` 환경변수 파싱, 계좌별 prefix 자격증명(`MOCK_APPKEY` 등) 로드, provider/broker 쌍 조립 |
| `state_machine.py` | 종목별 상태머신. `MANUAL_TRADING`/`MONITOR`/`AUTO_TRADING` + 장전/장중 가드 |
| `market_clock.py` | 장 단계(PRE_OPEN/OPEN/CLOSED). mock=수동 토글, live=실제 KST 시계 자동 판정. 차트 시간축 epoch 변환 헬퍼 |
| `models.py` | Pydantic 모델 + enum. 프런트와 주고받는 모든 페이로드의 단일 정의처 |
| `screener.py` | 상한가 종목 스크리너(D 종가 vs 직전 거래일 +29~30%). 거래일 탐색 + 합성 mock 소스 포함 |
| `strategy/ulc.py` | 상한가 따라잡기(ULC) 자동매매 엔진. 틱 기반 진입필터·분할매수·익절/손절/트레일링 |
| `providers/base.py` | `DataProvider` / `Broker` ABC. `VALID_INTERVALS=(3,5,10,30,60,1440)`, `DAY_INTERVAL=1440` |
| `providers/mock.py` | 자격증명 없는 합성 시뮬레이터(시드 기반 랜덤워크 봉 + 틱 + 즉시체결 브로커) |
| `providers/kiwoom_api.py` | 키움 REST/WebSocket **self-contained** 클라이언트. 외부 `kiwoom` 프로젝트를 런타임에 import 하지 않음 |
| `providers/kiwoom.py` | `kiwoom_api`를 `DataProvider`/`Broker` 계약에 맞추는 어댑터. 포지션 진실원 = 계좌 잔고(kt00018) |
| `providers/krx_api.py` | KRX Open API 전종목 일별시세 클라이언트(스크리너 전용). `KRX_OPEN_API_KEY` 필요 |

### 프런트 (`frontend/src/`)

| 파일 | 역할 |
|---|---|
| `store.jsx` | 전역 상태(Context) + WebSocket. 메시지의 `account` 필드로 계좌별 슬라이스에 라우팅 |
| `api.js` | REST 래퍼. 계좌 스코프는 `/api/{account}/...`, 전역은 `/api/market*`·`/api/screener/*` |
| `indicators.js` | SMA 계산(전체 재계산 `sma`, 틱 갱신용 O(period) `lastSma`) |
| `router.js` | 최소 해시 라우터(`#/`, `#/upper-limit`). react-router 의존 없음 |
| `App.jsx` | 라우팅 + 공통 헤더, 계좌 컬럼 레이아웃, 계좌 필터 세그먼트 |
| `pages/` | UpperLimitPage — 상한가 종목 조회/표 |
| `components/` | Chart, PriceTicker, StateButton, ManualTradePanel, AutoConfigForm, IntervalSelector, StockInput, StockPanel, MarketControls, AccountSummary, LogPanel, PageNav |

## 핵심 개념

- **상태머신**: `[*] → MANUAL_TRADING`. `PUSH`(장전에만 `MANUAL↔MONITOR`),
  `MARKET-OPEN`(`MONITOR→AUTO`), `MARKET-CLOSE`(모두 `→MANUAL` + 장 단계 `PRE_OPEN` 리셋),
  `POSITION-FLAT`(엔진 전량매도로 보유수량 0 → `AUTO→MANUAL`).
  장중 `MANUAL_TRADING`은 종착 — 나가는 전이 없음. **청산 버튼은 없다**(청산은 매매의 결과).
- **장 시계는 전 계좌 공유**: kiwoom 계좌가 하나라도 있으면 실제 KST 시계로 자동
  판정되고 수동 장 토글이 409로 거부된다. 수동 토글 데모는 `./dev.sh --demo`
  (또는 `ACCOUNTS=mock` + `MOCK_PROVIDER=mock`).
- **서버가 권위**: 상태머신·틱 스트림·이벤트는 서버가 관리하고 WS로 브로드캐스트한다.
- **영속화**: 계좌별 종목 목록은 `backend/state.json`(데모는 `state.demo.json`).
  재시작 시 `manager.restore()`로 복원.
- **자동매매 전략(ULC)**: `X`=전일 종가(상한가), `Z`=당일 시가. 진입필터 →
  시나리오(SC1/SC2/SC3)별 2~3분할 매수 → 평단 기준 익절/손절/트레일링.
  평단·수량 진실원 = 실체결 이벤트 + 계좌 보정. 상세: [docs/trading-strategy.md](docs/trading-strategy.md).
- **상한가 스크리너**: 시장 전체 데이터라 **계좌 스코프가 아니다**(`/api/screener/*`).
  키움에 전종목 일별시세 API가 없어 KRX Open API를 별도 소스로 쓴다. 키가 없으면
  합성 mock으로 자동 대체되며, 응답의 `source` 필드로 항상 구분되고 UI에 '데모
  데이터' 배지가 붙는다 — 합성값을 실데이터로 오인하는 게 가장 위험하다.

## 규칙 / 관례

- **코드가 진실원**. 문서(`docs/`, README)는 코드를 뒤따른다 — 불일치 시 코드 확인.
- 주석·커밋 메시지·UI 문자열은 **한국어**.
- provider는 mock/kiwoom 어느 쪽이든 `base.py`의 동일 계약을 지킨다. 새 데이터
  소스나 브로커 기능은 ABC에 먼저 반영.
- kiwoom API에는 명세서만 보면 빠지는 함정이 많다(주문 필드 시프트, 0B 거래량
  FID, 부호/제로패딩 가격, 종목코드 `A` 접두, WebSocket PING echo 등).
  `providers/kiwoom_api.py` 상단 주석에 정리돼 있으니 그쪽 수정 전 반드시 읽을 것.
- 봉/틱 시간축은 KST 벽시계를 UTC인 것처럼 환산한다(lightweight-charts가 UTC
  렌더링). `market_clock.chart_epoch` 참고. 시간축 회귀는 `test_chart_axis.py`.
- 새 기능은 `backend/tests/`에 회귀 테스트를 추가한다(WS 버스는 `FakeManager`로
  대체, `_persist`는 꺼서 `state.json`을 건드리지 않음 — `conftest.py` 참고).
- `dev.sh`는 서버를 `setsid`(새 세션)로 띄운다. 따라서 `trap`에 **`SIGHUP`이
  반드시 있어야** 한다 — 빠지면 SSH 끊김·터미널 종료 시 스크립트만 죽고 서버는
  SIGHUP 면역이라 고아로 남아 포트 8000을 영구 점유한다. 실제로 겪은 문제다.

## 참조 프로젝트

### `/home/rblue/work/kiwoom` — 키움 API 연동 모듈 (설계 참조용, 런타임 의존 없음)

초기 설계의 참고 코드였다. 현재 Bach는 키움 연동을 `providers/kiwoom_api.py`에
**self-contained**로 재구현했으므로 이 프로젝트를 import 하지 않는다.
전략 사양 원문(`doc/상한가 전략.md`, `doc/상한가 전략 시각화.md`,
`trading_config.yaml`)을 확인할 때만 참고한다.

## 문서 (`docs/`)

- [trading-strategy.md](docs/trading-strategy.md) — ULC 자동매매 전략 (구현 기준)
- [kiwoom_rest_api_full_v3.md](docs/kiwoom_rest_api_full_v3.md) — 키움 REST API 명세
- [remote-access-proxy-websocket.md](docs/remote-access-proxy-websocket.md) — HTTP 프록시 경유 시 WebSocket 끊김 원인/우회(SSH 포워딩)
