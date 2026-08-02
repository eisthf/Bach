import React from 'react'
import { StoreProvider, useStore } from './store'
import StockInput from './components/StockInput'
import StockPanel from './components/StockPanel'
import MarketControls from './components/MarketControls'
import AccountSummary from './components/AccountSummary'
import LogPanel from './components/LogPanel'

// 한 계좌(모의/실전)의 종목 입력 + 칩바 + 패널 목록. 실전(danger)은 위험 톤.
function AccountColumn({ account }) {
  const { stocks, order, hidden, compact, actions } = useStore()
  const acc = account.id
  const accStocks = stocks[acc] || {}
  const accOrder = order[acc] || []
  const accHidden = hidden[acc] || new Set()
  const accCompact = compact[acc] || new Set()

  const visibleCodes = accOrder.filter((c) => accStocks[c])
  const list = visibleCodes.filter((c) => !accHidden.has(c)).map((c) => accStocks[c])
  const manualCodes = visibleCodes.filter((c) => accStocks[c].state === 'MANUAL_TRADING')
  const allManualCompact =
    manualCodes.length > 0 && manualCodes.every((c) => accCompact.has(c))

  const [importing, setImporting] = React.useState(false)
  const [importMsg, setImportMsg] = React.useState('')
  const doImportHeld = async () => {
    setImporting(true)
    setImportMsg('')
    try {
      const added = await actions.importHeld(acc)
      setImportMsg(added.length ? `${added.length}개 추가됨` : '추가할 보유 종목 없음')
    } catch (e) {
      setImportMsg(String(e.message || e))
    } finally {
      setImporting(false)
    }
  }

  return (
    <section className={`account-col ${account.danger ? 'danger' : 'safe'}`}>
      <div className="account-col-head">
        <span className={`account-badge ${account.danger ? 'danger' : 'safe'}`}>
          {account.danger ? '⚠️ 실전' : account.label}
        </span>
        {!account.live && <span className="account-demo">데모 데이터</span>}
        <AccountSummary account={acc} />
      </div>

      <div className="toolbar-row">
        <StockInput account={acc} />
        <button className="import-held-btn" onClick={doImportHeld} disabled={importing}>
          {importing ? '가져오는 중…' : '보유 종목 가져오기'}
        </button>
        {importMsg && <span className="import-msg">{importMsg}</span>}
        {manualCodes.length > 0 && (
          <button
            className="import-held-btn"
            onClick={() => actions.setCompactFor(acc, manualCodes, !allManualCompact)}
          >
            {allManualCompact ? '수동매매 펼치기' : '수동매매 컴팩트'}
          </button>
        )}
      </div>

      {visibleCodes.length > 0 && (
        <div className="chip-bar">
          {visibleCodes.map((c) => {
            const s = accStocks[c]
            const off = accHidden.has(c)
            return (
              <button
                key={c}
                className={`stock-chip ${off ? 'off' : 'on'}`}
                onClick={() => actions.toggleVisible(acc, c)}
                title={off ? '클릭하면 차트 표시' : '클릭하면 차트 숨김'}
              >
                {s.name || c}
              </button>
            )
          })}
        </div>
      )}

      <div className="panels-col">
        {list.length === 0 && (
          <div className="empty-state">
            {visibleCodes.length === 0
              ? '종목코드를 추가하면 차트와 매매 패널이 나타납니다.'
              : '모든 종목이 숨김 상태입니다.'}
          </div>
        )}
        {list.map((s) => (
          <StockPanel key={s.code} account={acc} stock={s} />
        ))}
      </div>
    </section>
  )
}

// 계좌 보기 필터(세그먼트). 계좌 목록에서 동적 생성하며, 계좌가 하나면 숨긴다.
function AccountFilter() {
  const { accounts, accountView, actions } = useStore()
  if (accounts.length < 2) return null
  const tabs = [...accounts, { id: 'ALL', label: '모두' }]
  return (
    <div className="account-filter" role="group" aria-label="계좌 보기">
      {tabs.map((t) => (
        <button
          key={t.id}
          className={`acc-tab${accountView === t.id ? ' active' : ''}${t.danger ? ' danger' : ''}`}
          onClick={() => actions.setAccountView(t.id)}
        >
          {t.label}
        </button>
      ))}
    </div>
  )
}

function Dashboard() {
  const { accounts, accountView } = useStore()
  // 단독 선택 시 그 계좌가 전폭을 쓴다(차트가 넓어짐).
  const shown =
    accountView === 'ALL' ? accounts : accounts.filter((a) => a.id === accountView)
  return (
    <div className="app">
      <div className="app-top">
        <header className="app-header">
          <div className="brand">
            <span className="brand-mark">♪</span>
            <span className="brand-name">Bach</span>
            <span className="brand-sub">trading system</span>
          </div>
          <AccountFilter />
          <MarketControls />
        </header>
      </div>

      <main className="main-grid multi">
        <div className="accounts-row" style={{ '--acc-cols': Math.max(shown.length, 1) }}>
          {shown.map((a) => (
            <AccountColumn key={a.id} account={a} />
          ))}
        </div>
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
