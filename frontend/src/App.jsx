import React from 'react'
import { StoreProvider, useStore } from './store'
import StockInput from './components/StockInput'
import StockPanel from './components/StockPanel'
import MarketControls from './components/MarketControls'
import LogPanel from './components/LogPanel'

function Dashboard() {
  const { stocks, order } = useStore()
  // 추가된 순서대로(위→아래) 배치. 객체 키 순서가 아니라 명시적 order 사용.
  const list = order.map((c) => stocks[c]).filter(Boolean)
  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark">♪</span>
          <span className="brand-name">Bach</span>
          <span className="brand-sub">주식 거래</span>
        </div>
        <MarketControls />
      </header>

      <div className="toolbar-row">
        <StockInput />
      </div>

      <main className="main-grid">
        <section className="panels-col">
          {list.length === 0 && (
            <div className="empty-state">
              종목코드를 추가하면 차트와 매매 패널이 나타납니다. (예: 005930)
            </div>
          )}
          {list.map((s) => (
            <StockPanel key={s.code} stock={s} />
          ))}
        </section>
        <aside className="side-col">
          <LogPanel />
        </aside>
      </main>
    </div>
  )
}

export default function App() {
  return (
    <StoreProvider>
      <Dashboard />
    </StoreProvider>
  )
}
