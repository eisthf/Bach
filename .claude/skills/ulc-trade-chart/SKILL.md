---
name: ulc-trade-chart
description: 상한가 따라잡기(ULC) 실매매 기록을 종목·날짜별 3분봉·일봉 차트(HTML 한 장)로 만든다. 체결가 ✕·B/S 마커, MA5/10/20/60, X(전일 종가)·Z(시가)·평단선, 손익 요약 포함. "상한가 매매 차트", "ULC 매매 결과 차트", "매수 매도 지점 표시한 차트 보여줘" 같은 요청에 사용.
---

# ULC 실매매 차트

계좌 체결 기록 + 엔진 로그에서 상한가(ULC) 매매를 찾아, 키움 차트 API로 봉을 받아 HTML로 그린다.
**조회 API만 쓰고 주문은 내지 않는다.** 실계좌 자격증명(`backend/.env`)이 필요하다.

## 실행

```bash
cd backend && uv run python ../.claude/skills/ulc-trade-chart/ulc_trade_chart.py
```

옵션:
- `--since 2026-10-01` / `--until 2026-10-15`: 기간(포함)
- `--code 001520`: 종목만(여러 번 가능)
- `--account real`: 계좌 id(기본 `real`). 체결 파일은 `state.{계좌}.trades.json`
- `--out <path>`: 출력(기본 `research/ulc_trades/index.html`, git 미추적)
- `--refresh`: 봉 캐시(`backend/backtest_data/ulc_chart/`) 무시

Windows 콘솔에서 한글 출력이 깨지면 `PYTHONIOENCODING=utf-8`을 앞에 붙인다.

## 만든 뒤

1. 내장 브라우저로 `file:///<repo>/research/ulc_trades/index.html`을 열고 콘솔 오류가 없는지, 차트가 그려졌는지 확인한다.
   미리보기 패널은 처음에 폭 0으로 그릴 때가 있어 템플릿이 ResizeObserver로 범위를 다시 맞춘다.
   파일이 ~550KB를 넘으면 패널이 못 여니, 커지면 봉 수를 줄인다(일봉은 종목별 95봉 앞부터, 전일 3분봉은 60개만).
2. `SendUserFile`(display: render)로 HTML을 보낸다.
3. 요약: 건수, 승/패, 실현 손익 합계(수수료·세금 제외), 손절 종목, 주의 사항(미청산·수동 매도).

## 판정 규칙 (스크립트 동작)

- **ULC 매매** = 그날 그 종목에 엔진 메시지 `자동매매 시작 (X=…, Z=…)`가 있고 체결이 있는 경우.
  대금 양봉·구간 매매(`candle_trade` 이벤트)와 순수 수동매매는 이 메시지가 없어 자동 제외.
- 체결은 주문번호(`id`의 두 번째 칸)로 묶고, 엔진 메시지 순서대로 사유를 붙인다:
  매수 `N차 매수` → `1차/2차/3차`, 매도 `익절`/`트레일링 스탑`/`손절:`. 남는 주문은 `수동`.
- 매수 수량 = 매도 수량이면 청산(손익 산정), 아니면 `미청산`(그날 이후 매도는 반영 안 함).
- 체결 `time`은 차트 epoch(KST 벽시계를 UTC로 둔 값)라 봉 시각과 바로 비교된다. 3분봉 시각은 봉 시작 기준.
- 3분봉은 정규장(≤15:27 시작 봉)만, 앞에 직전 거래일 정규장 3분봉 60개를 붙여 MA60을 09:00부터 잇는다.

## 차트 표현 (Bach 프런트와 동일)

- 체결 건마다 캔들 안 체결가 위치에 ✕ (`frontend/src/tradeCrossPrimitive.js` 이식, 매수 `#d32f2f`/매도 `#1565c0`)
- 봉 위/아래 화살표 마커 `B 1차`, `S 익절` (일봉은 `B 수량 · 평단`)
- MA5 `#2962FF` / MA10 `#FF4FA3` / MA20 `#FF9800` / MA60 `#26A65B` (`frontend/src/indicators.js` `MA_LINES`)
- Bach 쪽 색·마커 규칙이 바뀌면 `template.html`도 맞춘다.

## 파일

- `ulc_trade_chart.py`: 판정·조회·HTML 생성
- `template.html`: lightweight-charts 4.2.0 페이지, `__DATA__` 자리에 JSON 주입
