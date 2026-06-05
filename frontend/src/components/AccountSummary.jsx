import React, { useEffect, useState } from 'react'
import { api } from '../api'

const fmt = (n) => (n == null ? '-' : Math.round(Number(n)).toLocaleString('ko-KR'))

// 계좌 요약(예수금/주문가능금액/평가금액/총자산) 헤더 바. 주기적 폴링.
// mock 등 미지원이면 null → 렌더 안 함.
export default function AccountSummary() {
  const [acc, setAcc] = useState(null)

  useEffect(() => {
    let stopped = false
    const load = () =>
      api.account().then((a) => { if (!stopped) setAcc(a) }).catch(() => {})
    load()
    const t = setInterval(load, 8000)
    return () => { stopped = true; clearInterval(t) }
  }, [])

  if (!acc) return null
  const pnl = acc.eval_pnl
  return (
    <div className="account-summary" title="증권사 계좌 요약 (8초마다 갱신)">
      <div className="acc-cell">
        <span className="acc-label">총자산</span>
        <span className="acc-val strong">{fmt(acc.total_asset)}</span>
      </div>
      <div className="acc-cell">
        <span className="acc-label">주문가능</span>
        <span className="acc-val">{fmt(acc.orderable)}</span>
      </div>
      <div className="acc-cell">
        <span className="acc-label">평가금액</span>
        <span className="acc-val">
          {fmt(acc.stock_eval)}
          {pnl != null && (
            <span className={`acc-pnl ${pnl >= 0 ? 'up' : 'down'}`}>
              {' '}({pnl >= 0 ? '+' : ''}{fmt(pnl)})
            </span>
          )}
        </span>
      </div>
    </div>
  )
}
