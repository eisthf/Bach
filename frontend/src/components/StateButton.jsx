import React from 'react'
import { useStore } from '../store'

const LABELS = {
  IDLE: '대기',
  MONITOR: '모니터',
  AUTO_TRADING: '자동매매',
}

// 현재 state를 표시하고 상태 전환 이벤트를 발생시키는 버튼 + [수동 전환].
// 장 종료 후·주말에는 다음 장 모니터를 미리 예약할 수 있고,
// 장중 IDLE(그날 자동매매를 마친 종목)만 종착 상태라 상태 버튼을 비활성화한다.
// 자동매매 중의 PUSH는 수동 전환이다(종목이 수동 매매 화면으로 옮겨간다). [수동 전환]은 어느 상태에서든 된다.
export default function StateButton({ account, stock }) {
  const { phase, actions } = useStore()
  const state = stock.state
  const isOpen = phase === 'OPEN'
  const auto = state === 'AUTO_TRADING'

  // 상태 전환이 의미 있는지 (백엔드 가드와 동일 규칙). 자동매매는 [수동 전환] 버튼이 맡는다.
  const canPush = state === 'IDLE' ? !isOpen : !auto

  const nextHint =
    state === 'IDLE'
      ? isOpen
        ? '오늘은 끝 · 장중 감시 불가'
        : phase === 'CLOSED'
        ? '→ 다음 장 모니터 예약'
        : '→ 모니터'
      : state === 'MONITOR'
      ? '→ 대기'
      : isOpen
      ? '자동 주문 중'
      : '다음 장에 이어감'

  const transfer = () => actions.transferToManual(account, stock.code)
    .catch((e) => window.alert(`수동 전환 실패: ${e.message || e}`))

  return (
    <div className="state-box">
      <button
        className={`state-btn state-${state.toLowerCase()}`}
        disabled={!canPush}
        onClick={() => actions.push(account, stock.code)}
        title={nextHint}
      >
        {/* 장외의 자동매매는 보유 중이라 다음 거래일로 이월된 종목이다. */}
        <span className="state-name">{LABELS[state] || state}{auto && !isOpen ? ' (이월)' : ''}</span>
        <span className="state-sub">{nextHint}</span>
      </button>
      <button
        className={`transfer-btn${auto ? ' warn' : ''}`}
        onClick={transfer}
        title="자동매매를 멈추고 이 종목을 수동 매매 화면으로 옮깁니다(되돌릴 수 없음)"
      >
        수동 전환
      </button>
    </div>
  )
}
