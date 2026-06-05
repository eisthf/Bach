import React from 'react'
import { useStore } from '../store'

const LABELS = {
  MANUAL_TRADING: '수동매매',
  MONITOR: '모니터',
  AUTO_TRADING: '자동매매',
}

// 현재 state를 표시하고 PUSH 이벤트를 발생시키는 버튼.
// 장중 MANUAL_TRADING은 종착 → 버튼 비활성.
export default function StateButton({ stock }) {
  const { phase, actions } = useStore()
  const state = stock.state
  const isPreOpen = phase === 'PRE_OPEN'

  // PUSH가 의미 있는 전이를 만드는지 (백엔드 가드와 동일 규칙)
  const canPush = state === 'MANUAL_TRADING' ? isPreOpen : true

  const nextHint =
    state === 'MANUAL_TRADING'
      ? isPreOpen
        ? '→ 모니터'
        : '장중 종착(전이 불가)'
      : state === 'MONITOR'
      ? '→ 수동매매'
      : '→ 수동매매(수동 전환)'

  return (
    <div className="state-box">
      <button
        className={`state-btn state-${state.toLowerCase()}`}
        disabled={!canPush}
        onClick={() => actions.push(stock.code)}
        title={nextHint}
      >
        <span className="state-name">{LABELS[state]}</span>
        <span className="state-sub">{nextHint}</span>
      </button>
    </div>
  )
}
