import React from 'react'
import { StoreProvider, useStore } from './store'
import StockInput from './components/StockInput'
import StockPanel from './components/StockPanel'
import MarketControls from './components/MarketControls'
import AccountSummary from './components/AccountSummary'
import LogPanel from './components/LogPanel'

function Dashboard() {
  const { stocks, order, hidden, compact, actions } = useStore()
  // 추가된 순서대로(위→아래) 배치. 숨김 종목은 패널에서 제외(목록엔 유지).
  const visibleCodes = order.filter((c) => stocks[c])
  const list = visibleCodes.filter((c) => !hidden.has(c)).map((c) => stocks[c])
  // 수동매매 종목 일괄 컴팩트 토글
  const manualCodes = visibleCodes.filter((c) => stocks[c].state === 'MANUAL_TRADING')
  const allManualCompact =
    manualCodes.length > 0 && manualCodes.every((c) => compact.has(c))
  const [importing, setImporting] = React.useState(false)
  const [importMsg, setImportMsg] = React.useState('')
  const doImportHeld = async () => {
    setImporting(true)
    setImportMsg('')
    try {
      const added = await actions.importHeld()
      setImportMsg(added.length ? `${added.length}개 추가됨` : '추가할 보유 종목 없음')
    } catch (e) {
      setImportMsg(String(e.message || e))
    } finally {
      setImporting(false)
    }
  }
  return (
    <div className="app">
      <div className="app-top">
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark">♪</span>
          <span className="brand-name">Bach</span>
          <span className="brand-sub">trading system</span>
        </div>
        <AccountSummary />
        <MarketControls />
      </header>

      <div className="toolbar-row">
        <StockInput />
        <button
          className="import-held-btn"
          onClick={doImportHeld}
          disabled={importing}
          title="증권사 계좌의 보유 종목 중 화면에 없는 것을 가져옵니다"
        >
          {importing ? '가져오는 중…' : '보유 종목 가져오기'}
        </button>
        {importMsg && <span className="import-msg">{importMsg}</span>}
        {manualCodes.length > 0 && (
          <button
            className="import-held-btn"
            onClick={() => actions.setCompactFor(manualCodes, !allManualCompact)}
            title="수동매매 종목을 일괄로 컴팩트/펼치기"
          >
            {allManualCompact ? '수동매매 펼치기' : '수동매매 컴팩트'}
          </button>
        )}
      </div>

      {visibleCodes.length > 0 && (
        <div className="chip-bar">
          {visibleCodes.map((c) => {
            const s = stocks[c]
            const off = hidden.has(c)
            return (
              <button
                key={c}
                className={`stock-chip ${off ? 'off' : 'on'}`}
                onClick={() => actions.toggleVisible(c)}
                title={off ? '클릭하면 차트 표시' : '클릭하면 차트 숨김'}
              >
                {s.name || c}
              </button>
            )
          })}
        </div>
      )}
      </div>

      <main className="main-grid">
        <section className="panels-col">
          {list.length === 0 && (
            <div className="empty-state">
              {visibleCodes.length === 0
                ? '종목코드를 추가하면 차트와 매매 패널이 나타납니다. (예: 005930)'
                : '모든 종목이 숨김 상태입니다. 위 칩을 눌러 차트를 다시 표시하세요.'}
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
