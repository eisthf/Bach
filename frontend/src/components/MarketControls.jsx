import React from 'react'
import { useStore } from '../store'

const PHASE_LABEL = { PRE_OPEN: '장전', OPEN: '장중', CLOSED: '장종료' }

// 장 제어. live(자동) 모드에선 실제 KST 시계가 장을 열고 닫으므로 수동 버튼을
// 감추고 단계만 표시한다. mock(수동) 모드에선 데모용 토글 버튼을 노출한다.
export default function MarketControls() {
  const { phase, marketAuto, connected, actions } = useStore()
  return (
    <div className="market-controls">
      <span className={`conn ${connected ? 'on' : 'off'}`} title="WebSocket">
        ● {connected ? '연결됨' : '연결끊김'}
      </span>
      <span className={`phase phase-${phase.toLowerCase()}`}>장: {PHASE_LABEL[phase]}</span>
      {marketAuto ? (
        <span className="phase-auto" title="실제 KST 시계로 자동 판정 (09:00~15:30)">
          🕘 실시간 자동
        </span>
      ) : (
        <>
          <button onClick={() => actions.marketOpen()} disabled={phase === 'OPEN'}>
            장 시작
          </button>
          <button onClick={() => actions.marketClose()} disabled={phase === 'CLOSED'}>
            장 종료
          </button>
          <button onClick={() => actions.marketReset()} className="ghost">
            초기화
          </button>
        </>
      )}
    </div>
  )
}
