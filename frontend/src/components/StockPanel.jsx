import React, { useState } from 'react'
import { useStore } from '../store'
import Chart from './Chart'
import IntervalSelector from './IntervalSelector'
import PriceTicker from './PriceTicker'
import StateButton from './StateButton'
import ManualTradePanel from './ManualTradePanel'
import AutoConfigForm from './AutoConfigForm'

// 종목 1개 카드: 차트 + 시세 + 상태버튼 + 수동/자동 패널.
export default function StockPanel({ stock }) {
  const { ticks, actions } = useStore()
  const [interval, setInterval] = useState(3) // 기본 3분봉
  const tick = ticks[stock.code]

  return (
    <div className="stock-panel">
      <div className="panel-header">
        <div className="panel-id">
          <span className="panel-code">{stock.code}</span>
          <span className="panel-name">{stock.name}</span>
        </div>
        <PriceTicker tick={tick} />
        <button
          className="remove-btn"
          onClick={() => actions.removeStock(stock.code)}
          title="종목 제거"
        >
          ✕
        </button>
      </div>

      <div className="chart-toolbar">
        <IntervalSelector value={interval} onChange={setInterval} />
        <StateButton stock={stock} />
      </div>

      <Chart code={stock.code} interval={interval} tick={tick} />

      <div className="panels">
        <ManualTradePanel stock={stock} tick={tick} />
        <AutoConfigForm stock={stock} />
      </div>
    </div>
  )
}
