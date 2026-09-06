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

# SIGHUP 이 반드시 포함돼야 한다. 서버는 아래에서 setsid 로 '새 세션'에 띄우므로
# 터미널이 사라져도 SIGHUP 을 받지 않고 살아남는다(그게 setsid 의 목적이다).
# 그런데 이 스크립트가 SIGHUP 을 안 잡으면, SSH 가 끊기거나 터미널 창을 닫는
# 순간 스크립트만 즉사하고 cleanup 이 돌지 않는다 → 서버는 부모 없이(PPID=1)
# 영원히 남아 포트 8000 을 점유한다. 이 VM 은 SSH 로 접속하는 환경이라 연결
# 끊김이 흔해서 실제로 이 경로로 고아가 쌓였다.
trap cleanup SIGINT SIGTERM SIGHUP

# ---------------------------------------------------------------------------
# 포트 선점 검사
#
# 검사 없이 진행하면 vite(5173)만 뜨고 백엔드는 'Address already in use' 로
# 실패한다 — 화면은 열리는데 아무것도 안 되고, 원인은 스크롤을 한참 거슬러
# 올라가야 보인다. 시작 전에 멈추고 무엇이 잡고 있는지 알려준다.
#
# 자동으로 죽이지는 않는다: 남아 있는 백엔드가 실전 계좌(ACCOUNTS 에 real)로
# 떠 있을 수 있어, 무엇인지 확인하고 사람이 종료하는 편이 안전하다.
# ---------------------------------------------------------------------------
port_pids() {
  command -v ss >/dev/null 2>&1 || return 0
  ss -ltnp 2>/dev/null | grep -E ":$1[[:space:]]" \
    | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
}

check_ports() {
  local busy=0 port pids pid pgid pgids=""
  for port in 8000 5173; do
    pids="$(port_pids "$port")"
    [ -z "$pids" ] && continue
    busy=1
    echo "⚠️  포트 $port 가 이미 사용 중입니다:" >&2
    for pid in $pids; do
      # 소켓을 쥔 PID 가 프로세스 그룹 리더가 아닌 경우가 많다(uvicorn --reload
      # 는 감시 프로세스와 워커를 따로 둔다). 그룹째 정리해야 하므로 PGID 를
      # 함께 뽑아 안내한다.
      pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')"
      echo "     PID $pid (그룹 ${pgid:-?})  시작: $(ps -o lstart= -p "$pid" 2>/dev/null | xargs)" >&2
      echo "         $(ps -o args= -p "$pid" 2>/dev/null | cut -c1-80)" >&2
      [ -n "$pgid" ] && case " $pgids " in *" $pgid "*) ;; *) pgids="$pgids $pgid" ;; esac
    done
  done
  [ "$busy" -eq 0 ] && return 0

  echo "" >&2
  echo "   이전 실행이 남긴 프로세스일 수 있습니다(SSH 끊김·터미널 종료 시 발생)." >&2
  if [ -n "$pgids" ]; then
    echo "   확인 후 프로세스 그룹째 종료하세요:" >&2
    echo "" >&2
    for pgid in $pgids; do
      echo "       kill -TERM -$pgid" >&2
    done
    echo "" >&2
    echo "   ⚠️ 실전 계좌가 붙은 백엔드일 수 있습니다. 종료 전에 확인하세요:" >&2
    for pgid in $pgids; do
      echo "       tr '\\0' '\\n' < /proc/$pgid/environ | grep ACCOUNTS" >&2
    done
  fi
  exit 1
}

check_ports

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
