#!/bin/bash
# Bach 개발 서버 시작 스크립트 (백엔드 + 프론트 동시 실행)
#
#   ./dev.sh          .env 설정 그대로 (실거래 계좌 포함 가능)
#   ./dev.sh --demo   합성 mock 계좌 1개만. 장 시작/종료를 수동 토글 가능
#
# --demo 가 필요한 이유: 장 시계는 전 계좌 공유이고, kiwoom 계좌가 하나라도
# 있으면 실제 KST 시각으로 자동 판정되어 수동 장 토글이 409로 거부된다.
# 장 마감 시각이나 주말에 UI·상태머신·자동매매를 시연하려면 kiwoom 계좌 없이
# 띄워야 한다. .env 는 건드리지 않고 환경변수로 덮어쓴다(python-dotenv 는
# 기존 환경변수를 덮어쓰지 않으므로 이쪽이 우선한다).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DEMO=0
for arg in "$@"; do
  case "$arg" in
    --demo) DEMO=1 ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "알 수 없는 옵션: $arg (사용법: $0 [--demo])" >&2
      exit 1 ;;
  esac
done

PIDS=()

cleanup() {
  echo ""
  echo "🛑 서버 종료 중..."
  # uvicorn --reload 는 감시 프로세스가 자식을 두고, SIGTERM 만으로는 소켓을
  # 놓지 않고 남는 경우가 있다(그렇게 남은 프로세스가 포트 8000 을 계속 점유해
  # 다음 실행이 'Address already in use' 로 실패한다). 프로세스 그룹째 보내고,
  # 그래도 남으면 SIGKILL 로 확실히 정리한다.
  for pid in "${PIDS[@]}"; do
    kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  done
  for _ in 1 2 3 4 5 6; do
    sleep 0.5
    local alive=0
    for pid in "${PIDS[@]}"; do kill -0 "$pid" 2>/dev/null && alive=1; done
    [ "$alive" -eq 0 ] && break
  done
  for pid in "${PIDS[@]}"; do
    kill -0 "$pid" 2>/dev/null || continue
    kill -KILL -"$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  echo "✅ 종료됨"
  exit 0
}

trap cleanup SIGINT SIGTERM

# 백엔드 초기 셋업 (1회만)
if [ ! -d "backend/.venv" ]; then
  echo "📦 백엔드 초기 셋업..."
  (cd backend && uv venv --python 3.11 && uv pip install -e . && cp -n .env.example .env)
  echo ""
fi

# 프론트 초기 셋업 (1회만)
if [ ! -d "frontend/node_modules" ]; then
  echo "📦 프론트 초기 셋업..."
  (cd frontend && npm install)
  echo ""
fi

echo "========================================="
if [ "$DEMO" -eq 1 ]; then
  echo "🧪 Bach 개발 서버 시작 — 데모 모드"
  echo "   합성 mock 계좌 1개. 실주문 없음. 장 토글 수동."
else
  echo "🚀 Bach 개발 서버 시작"
fi
echo "========================================="

# 백엔드 실행.
# setsid 로 각자 프로세스 그룹을 갖게 해, 종료 시 그룹째 정리할 수 있게 한다
# (uvicorn --reload 는 감시 프로세스가 자식을 두므로 대표 PID 만 죽이면 남는다).
if [ "$DEMO" -eq 1 ]; then
  # 합성 mock 단독 → 공유 시계가 수동 모드(auto=false)라 장 시작/종료 토글 가능.
  # 실거래 계좌를 배제하므로 실주문 위험이 없고, 로컬 시연이라 토큰 인증도 끈다.
  # 상태 파일도 분리해 평소 종목 목록(state.json)을 덮어쓰지 않는다.
  (cd backend && ACCOUNTS=mock MOCK_PROVIDER=mock API_TOKEN= \
     BACH_STATE_MOCK="$SCRIPT_DIR/backend/state.demo.json" \
     exec setsid uv run uvicorn app.main:app --reload) &
else
  (cd backend && exec setsid uv run uvicorn app.main:app --reload) &
fi
PIDS+=($!)
echo "✅ 백엔드 실행 중 (포트 8000, PID: ${PIDS[-1]})"

# 프론트 실행
(cd frontend && exec setsid npm run dev) &
PIDS+=($!)
echo "✅ 프론트 실행 중 (포트 5173, PID: ${PIDS[-1]})"

echo ""
echo "📱 접속 주소: http://localhost:5173"
if [ "$DEMO" -eq 1 ]; then
  echo ""
  echo "   데모 흐름: 종목 추가 → 칩 PUSH로 '모니터' → 우상단 '장 시작'"
  echo "   → 자동매매 진입. 상태는 backend/state.demo.json 에만 저장됨."
fi
echo ""
echo "⏹️  종료: Ctrl+C 입력"
echo "========================================="
echo ""

# 대기 (Ctrl+C까지)
wait
