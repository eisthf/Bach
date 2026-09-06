import React from 'react'
import { useStore } from '../store'

const LABELS = {
  MANUAL_TRADING: '수동매매',
  MONITOR: '모니터',
  AUTO_TRADING: '자동매매',
}

// 현재 state를 표시하고 상태 전환 이벤트를 발생시키는 버튼.
// 장 종료 후·주말에는 다음 장 모니터를 미리 예약할 수 있고,
// 장중 MANUAL_TRADING만 종착 상태라 버튼을 비활성화한다.
export default function StateButton({ account, stock }) {
  const { phase, actions } = useStore()
  const state = stock.state
  const isOpen = phase === 'OPEN'

  // 상태 전환이 의미 있는지 (백엔드 가드와 동일 규칙)
  const canPush = state === 'MANUAL_TRADING' ? !isOpen : true

  const nextHint =
    state === 'MANUAL_TRADING'
      ? isOpen
        ? '장중 전환 불가'
        : phase === 'CLOSED'
        ? '→ 다음 장 모니터 예약'
        : '→ 모니터'
      : state === 'MONITOR'
      ? '→ 수동매매'
      : '→ 수동매매(수동 전환)'

  return (
    <div className="state-box">
      <button
        className={`state-btn state-${state.toLowerCase()}`}
        disabled={!canPush}
        onClick={() => actions.push(account, stock.code)}
        title={nextHint}
      >
        <span className="state-name">{LABELS[state]}</span>
        <span className="state-sub">{nextHint}</span>
      </button>
    </div>
  )
}
