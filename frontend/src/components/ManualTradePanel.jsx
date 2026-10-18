import React, { useState } from 'react'
import { useStore } from '../store'

const fmt = (n) => Number(n || 0).toLocaleString('ko-KR')

// 수동매매: 매수 금액(원)·매도 수량(주) 입력 + 버튼. 수동 매매 화면 전용(자동 손절·익절은 ManualExits).
// readOnly 면 보유·평가손익·미체결만 보인다([상한가 매매] 화면 — 자동매매 전용이라 주문 버튼이 없다).
// position/verified 는 계좌 잔고(진실원).
export default function ManualTradePanel({ account, code, position, verified = true, tick, readOnly = false }) {
  const { actions, orders } = useStore()
  const [amount, setAmount] = useState(500000)
  const [qty, setQty] = useState(0)  // 기본 0 — '전량' 버튼으로 보유수량 채움
  const [msg, setMsg] = useState('')
  const pending = (orders[account] || {})[code] || []

  const pos = position || { quantity: 0, avg_price: 0 }
  const price = tick?.price
  const pnlPct = verified && pos.quantity > 0 && pos.avg_price > 0 && price > 0
    ? (price - pos.avg_price) / pos.avg_price * 100
    : null
  const pnlPctLabel = pnlPct != null && Number.isFinite(pnlPct)
    ? `${pnlPct.toLocaleString('ko-KR', { minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: 'exceptZero' })}%`
    : '—'
  // 현재가가 없으면 주식수를 계산할 수 없음 → null(표시는 '—'주)
  const estShares = price ? Math.floor(amount / price) : null

  const doBuy = async () => {
    const r = await actions.buy(account, code, Number(amount))
    setMsg(r.message)
  }
  const doSell = async () => {
    const r = await actions.sell(account, code, Number(qty))
    setMsg(r.message)
  }

  return (
    <div className={`manual-panel${readOnly ? ' read-only' : ''}`}>
      <div className="panel-title">{readOnly ? '계좌 보유' : '수동매매'}</div>
      <div className="position-row">
        <span>보유 {verified ? fmt(pos.quantity) : '확인 불가'}주</span>
        <span>평단 {verified ? fmt(Math.round(pos.avg_price)) : '—'}</span>
        <span className={pos.quantity > 0 && price >= pos.avg_price ? 'up' : 'down'}>
          평가손익 {verified && price ? fmt(Math.round((price - pos.avg_price) * pos.quantity)) : '-'}
          {' '}<span title="평단 대비 평가수익률 · 수수료·세금 미반영">({pnlPctLabel})</span>
        </span>
      </div>

      {!readOnly && (
        <>
          <div className="trade-row">
            <label>매수금액(원)</label>
            <input type="number" step="10000" value={amount} onChange={(e) => setAmount(e.target.value)} />
            <span className="hint">≈ {estShares == null ? '—' : fmt(estShares)}주</span>
            <button className="buy-btn" onClick={doBuy}>매수</button>
          </div>

          <div className="trade-row">
            <label>매도수량(주)</label>
            <input type="number" step="1" value={qty} onChange={(e) => setQty(e.target.value)} />
            <button className="sell-all" disabled={!verified} onClick={() => setQty(pos.quantity)} title="보유 전량">
              전량
            </button>
            <button className="sell-btn" disabled={pos.quantity <= 0 || Number(qty) <= 0} onClick={doSell}>
              매도
            </button>
          </div>
        </>
      )}

      {pending.length > 0 && (
        <div className="pending-orders">
          <div className="pending-title">미체결</div>
          {pending.map((o, i) => (
            <div key={o.order_no || i} className="pending-row">
              <span className={`pending-side ${o.side === 'sell' ? 'down' : 'up'}`}>
                {o.side === 'sell' ? '매도' : '매수'}
              </span>
              <span className="pending-detail">
                미체결 {fmt(o.unfilled)} / 주문 {fmt(o.qty)}주 {o.order_type || ''}
                {o.price > 0 ? ` @ ${fmt(o.price)}` : ''}
                {o.filled > 0 ? ` · 체결 ${fmt(o.filled)}` : ''}
              </span>
              <span className="pending-status">{o.status}</span>
            </div>
          ))}
        </div>
      )}
      {msg && <div className="trade-msg">{msg}</div>}
      {readOnly && <div className="lock-note">수동 매수·매도는 [수동 전환] 뒤 수동 매매 화면에서</div>}
    </div>
  )
}
