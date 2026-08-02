import React from 'react'
import { useStore } from '../store'

const time = (t) => new Date(t).toLocaleTimeString('ko-KR', { hour12: false })

// 체결/상태변화/이벤트 로그 스트림. 계좌 라벨 배지로 모의/실전을 구분.
export default function LogPanel() {
  const { logs, accounts, accountView } = useStore()
  const labelOf = (id) => accounts.find((a) => a.id === id)
  // 계좌 필터에 맞춰 로그도 좁힌다. 계좌 없는 전역 로그(장 이벤트)는 항상 표시.
  const shown =
    accountView === 'ALL'
      ? logs
      : logs.filter((l) => !l.account || l.account === accountView)
  return (
    <div className="log-panel">
      <div className="log-title">이벤트 로그</div>
      <div className="log-list">
        {shown.length === 0 && <div className="log-empty">이벤트 없음</div>}
        {shown
          .slice()
          .reverse()
          .map((l, i) => {
            const a = l.account ? labelOf(l.account) : null
            return (
              <div key={i} className="log-row">
                <span className="log-time">{time(l.t)}</span>
                {a && (
                  <span className={`log-acc ${a.danger ? 'danger' : 'safe'}`}>{a.label}</span>
                )}
                <span className="log-text">{l.text}</span>
              </div>
            )
          })}
      </div>
    </div>
  )
}
