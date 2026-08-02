#!/bin/bash
# Bach 개발 서버 시작 스크립트 (백엔드 + 프론트 동시 실행)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PIDS=()

cleanup() {
  echo ""
  echo "🛑 서버 종료 중..."
  for pid in "${PIDS[@]}"; do
    kill $pid 2>/dev/null || true
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
echo "🚀 Bach 개발 서버 시작"
echo "========================================="

# 백엔드 실행
(cd backend && uv run uvicorn app.main:app --reload) &
PIDS+=($!)
echo "✅ 백엔드 실행 중 (포트 8000, PID: ${PIDS[-1]})"

# 프론트 실행
(cd frontend && npm run dev) &
PIDS+=($!)
echo "✅ 프론트 실행 중 (포트 5173, PID: ${PIDS[-1]})"

echo ""
echo "📱 접속 주소: http://localhost:5173"
echo ""
echo "⏹️  종료: Ctrl+C 입력"
echo "========================================="
echo ""

# 대기 (Ctrl+C까지)
wait
