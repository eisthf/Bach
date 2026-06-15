import React from 'react'
import { useStore } from '../store'

const time = (t) => new Date(t).toLocaleTimeString('ko-KR', { hour12: false })

// 체결/상태변화/이벤트 로그 스트림. 계좌 라벨 배지로 모의/실전을 구분.
export default function LogPanel() {
  const { logs, accounts } = useStore()
  const labelOf = (id) => accounts.find((a) => a.id === id)
  return (
    <div className="log-panel">
      <div className="log-title">이벤트 로그</div>
      <div className="log-list">
        {logs.length === 0 && <div className="log-empty">이벤트 없음</div>}
        {logs
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
