import React from 'react'
import { useStore } from '../store'

const PHASE_LABEL = { PRE_OPEN: '장전', OPEN: '장중', CLOSED: '장종료' }

// 데모용 장 제어. 실거래에선 실제 시계가 이 이벤트를 발생시킨다.
export default function MarketControls() {
  const { phase, connected, actions } = useStore()
  return (
    <div className="market-controls">
      <span className={`conn ${connected ? 'on' : 'off'}`} title="WebSocket">
        ● {connected ? '연결됨' : '연결끊김'}
      </span>
      <span className={`phase phase-${phase.toLowerCase()}`}>장: {PHASE_LABEL[phase]}</span>
      <button onClick={() => actions.marketOpen()} disabled={phase === 'OPEN'}>
        장 시작
      </button>
      <button onClick={() => actions.marketClose()} disabled={phase === 'CLOSED'}>
        장 종료
      </button>
      <button onClick={() => actions.marketReset()} className="ghost">
        초기화
      </button>
    </div>
  )
}
