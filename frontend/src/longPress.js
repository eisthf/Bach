// 길게 누르기(마우스·터치 공통). 종목 칩: 짧게 누르면 보이기/숨기기, 길게 누르면 그 종목만 보기.
// 칩마다 훅을 두지 않으려고 누르는 중 상태를 모듈에 둔다 — 포인터는 한 번에 하나만 누른다.
// 누르는 동안 시세 틱으로 다시 렌더돼도 상태가 유지된다.
const HOLD_MS = 500
let timer = null
let fired = false

const cancel = () => { clearTimeout(timer); timer = null }

export function longPress(onLong, onClick) {
  return {
    onPointerDown: (e) => {
      if (e.button !== 0) return
      cancel()
      fired = false
      timer = setTimeout(() => { timer = null; fired = true; onLong() }, HOLD_MS)
    },
    onPointerUp: cancel,
    onPointerLeave: cancel,
    // 칩바를 손가락으로 스크롤하면 pointercancel 이 온다 — 길게 누르기 아님.
    onPointerCancel: cancel,
    // 길게 누른 뒤 손을 떼면 click 이 이어 온다 — 그건 삼킨다.
    onClick: () => {
      if (fired) { fired = false; return }
      onClick()
    },
    // 터치 길게 누르기의 컨텍스트 메뉴(모바일)를 막는다.
    onContextMenu: (e) => { if (fired || timer) e.preventDefault() },
  }
}
