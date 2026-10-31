import React from 'react'
import { useStore } from '../store'
import Chart from './Chart'
import IntervalSelector from './IntervalSelector'
import PriceTicker from './PriceTicker'
import { useChartInterval } from '../chartPrefs'
import StateButton from './StateButton'
import ManualTradePanel from './ManualTradePanel'
import AutoConfigForm from './AutoConfigForm'

// [상한가 매매] 종목 1개 카드: 차트 + 시세 + 상태버튼·수동 전환 + 계좌 보유(읽기 전용) + 자동매매 설정.
// 수동 매수·매도는 없다 — [수동 전환]하면 종목이 수동 매매 화면으로 옮겨간다.
// 컴팩트 모드: 자동매매설정을 숨기고 차트(좌)+보유(우)를 나란히 → 카드 높이 절약.
export default function StockPanel({ account, stock }) {
  const { ticks, compact, actions } = useStore()
  const prefKey = `ulc:${account}:${stock.code}`
  const [interval, setInterval] = useChartInterval(prefKey) // 기본 일봉
  const tick = (ticks[account] || {})[stock.code]
  const isCompact = (compact[account] || new Set()).has(stock.code)

  const toolbar = (
    <div className="chart-toolbar">
      <IntervalSelector value={interval} onChange={setInterval} />
      <StateButton account={account} stock={stock} />
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
            onClick={() => actions.toggleCompact(account, stock.code)}
            title={isCompact ? '펼치기 (자동매매 설정 표시)' : '컴팩트 (차트+보유만)'}
          >
            {isCompact ? '펼치기' : '컴팩트'}
          </button>
          <button
            className="remove-btn"
            onClick={() => actions.removeStock(account, stock.code)}
            title="종목 제거"
          >
            ✕
          </button>
        </div>
      </div>

      {stock.recovery_notice && <div className="recovery-notice" role="alert">{stock.recovery_notice}</div>}
      {stock.position_verified === false && <div className="recovery-notice" role="alert">잔고 조회 실패 — 표시 수량을 신뢰하지 말고 계좌에서 확인하세요.</div>}

      {isCompact ? (
        <div className="compact-body">
          <div className="compact-chart">
            {toolbar}
            <Chart prefKey={prefKey} account={account} code={stock.code} interval={interval} tick={tick} height={260} />
          </div>
          <ManualTradePanel account={account} code={stock.code} position={stock.position}
            verified={stock.position_verified !== false} tick={tick} readOnly />
        </div>
      ) : (
        <>
          {toolbar}
          <Chart prefKey={prefKey} account={account} code={stock.code} interval={interval} tick={tick} />
          <div className="panels">
            <ManualTradePanel account={account} code={stock.code} position={stock.position}
              verified={stock.position_verified !== false} tick={tick} readOnly />
            <AutoConfigForm account={account} stock={stock} />
          </div>
        </>
      )}
    </div>
  )
}
