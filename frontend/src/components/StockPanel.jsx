import React, { useState } from 'react'
import { useStore } from '../store'
import Chart from './Chart'
import IntervalSelector from './IntervalSelector'
import PriceTicker from './PriceTicker'
import StateButton from './StateButton'
import ManualTradePanel from './ManualTradePanel'
import AutoConfigForm from './AutoConfigForm'

// 종목 1개 카드: 차트 + 시세 + 상태버튼 + 수동/자동 패널.
// 컴팩트 모드: 자동매매설정을 숨기고 차트(좌)+매매(우)를 나란히 → 카드 높이 절약.
export default function StockPanel({ stock }) {
  const { ticks, compact, actions } = useStore()
  const [interval, setInterval] = useState(3) // 기본 3분봉
  const tick = ticks[stock.code]
  const isCompact = compact.has(stock.code)

  const toolbar = (
    <div className="chart-toolbar">
      <IntervalSelector value={interval} onChange={setInterval} />
      <StateButton stock={stock} />
    </div>
  )

  return (
    <div className={`stock-panel ${isCompact ? 'compact' : ''}`}>
      <div className="panel-header">
        <div className="panel-id">
          <span className="panel-code">{stock.code}</span>
          <span className="panel-name">{stock.name}</span>
        </div>
        <PriceTicker tick={tick} />
        <div className="panel-actions">
          <button
            className="layout-btn"
            onClick={() => actions.toggleCompact(stock.code)}
            title={isCompact ? '펼치기 (자동매매 설정 표시)' : '컴팩트 (차트+매매만)'}
          >
            {isCompact ? '펼치기' : '컴팩트'}
          </button>
          <button
            className="remove-btn"
            onClick={() => actions.removeStock(stock.code)}
            title="종목 제거"
          >
            ✕
          </button>
        </div>
      </div>

      {isCompact ? (
        <div className="compact-body">
          <div className="compact-chart">
            {toolbar}
            <Chart code={stock.code} interval={interval} tick={tick} height={260} />
          </div>
          <ManualTradePanel stock={stock} tick={tick} />
        </div>
      ) : (
        <>
          {toolbar}
          <Chart code={stock.code} interval={interval} tick={tick} />
          <div className="panels">
            <ManualTradePanel stock={stock} tick={tick} />
            <AutoConfigForm stock={stock} />
          </div>
        </>
      )}
    </div>
  )
}
