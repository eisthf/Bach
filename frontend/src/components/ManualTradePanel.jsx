import React, { useState } from 'react'
import { useStore } from '../store'

const fmt = (n) => Number(n || 0).toLocaleString('ko-KR')

// 수동매매: 매수 금액(원)·매도 수량(주) 입력 + 버튼.
// MANUAL_TRADING 상태에서만 활성.
export default function ManualTradePanel({ stock, tick }) {
  const { actions } = useStore()
  const [amount, setAmount] = useState(500000)
  const [qty, setQty] = useState(0)  // 기본 0 — '전량' 버튼으로 보유수량 채움
  const [msg, setMsg] = useState('')

  const enabled = stock.state === 'MANUAL_TRADING'
  const pos = stock.position
  const price = tick?.price
  const estShares = price ? Math.floor(amount / price) : 0

  const doBuy = async () => {
    const r = await actions.buy(stock.code, Number(amount))
    setMsg(r.message)
  }
  const doSell = async () => {
    const r = await actions.sell(stock.code, Number(qty))
    setMsg(r.message)
  }

  return (
    <div className={`manual-panel ${enabled ? '' : 'disabled'}`}>
      <div className="panel-title">수동매매</div>
      <div className="position-row">
        <span>보유 {fmt(pos.quantity)}주</span>
        <span>평단 {fmt(Math.round(pos.avg_price))}</span>
        <span className={pos.quantity > 0 && price >= pos.avg_price ? 'up' : 'down'}>
          평가손익 {price ? fmt(Math.round((price - pos.avg_price) * pos.quantity)) : '-'}
        </span>
      </div>

      <div className="trade-row">
        <label>매수금액(원)</label>
        <input
          type="number"
          step="10000"
          value={amount}
          disabled={!enabled}
          onChange={(e) => setAmount(e.target.value)}
        />
        <span className="hint">≈ {fmt(estShares)}주</span>
        <button className="buy-btn" disabled={!enabled} onClick={doBuy}>
          매수
        </button>
      </div>

      <div className="trade-row">
        <label>매도수량(주)</label>
        <input
          type="number"
          step="1"
          value={qty}
          disabled={!enabled}
          onChange={(e) => setQty(e.target.value)}
        />
        <button
          className="sell-all"
          disabled={!enabled}
          onClick={() => setQty(pos.quantity)}
          title="보유 전량"
        >
          전량
        </button>
        <button
          className="sell-btn"
          disabled={!enabled || pos.quantity <= 0 || Number(qty) <= 0}
          onClick={doSell}
        >
          매도
        </button>
      </div>

      {msg && <div className="trade-msg">{msg}</div>}
      {!enabled && <div className="lock-note">자동매매/모니터 상태에서는 잠금</div>}
    </div>
  )
}
