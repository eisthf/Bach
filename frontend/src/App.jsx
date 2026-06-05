import React from 'react'
import { StoreProvider, useStore } from './store'
import StockInput from './components/StockInput'
import StockPanel from './components/StockPanel'
import MarketControls from './components/MarketControls'
import AccountSummary from './components/AccountSummary'
import LogPanel from './components/LogPanel'

function Dashboard() {
  const { stocks, order, actions } = useStore()
  // 추가된 순서대로(위→아래) 배치. 객체 키 순서가 아니라 명시적 order 사용.
  const list = order.map((c) => stocks[c]).filter(Boolean)
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
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark">♪</span>
          <span className="brand-name">Bach</span>
          <span className="brand-sub">주식 거래</span>
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
