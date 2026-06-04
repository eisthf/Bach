# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

주식 거래 웹 앱. `/home/rblue/work/kiwoom`의 키움 API 연동 코드를 참고하여 구현한다.

## Related Projects

### `/home/rblue/work/kiwoom` — 키움 API 연동 모듈 (참조용)

이 프로젝트의 핵심 참고 코드. 다음 내용을 활용한다:

- **WebSocket 실시간 체결 데이터** (`src/auto_trading.py`): 종목별 가격 틱 수신 (`type 0B`)
- **REST API 주문 실행** (`place_buy_order`, `place_sell_order`)
- **시장 데이터 조회** (`src/utils.py`): pykrx, KRX Open API 등 OHLCV 데이터 소스
- **자동매매 전략** (`src/trading_strategies.py`): `TradingStrategy` ABC 및 구현체들
- **설정 구조** (`trading_config.yaml`): 종목별 전략 파라미터
