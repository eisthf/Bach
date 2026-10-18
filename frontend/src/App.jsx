import React from 'react'
import { StoreProvider, useStore } from './store'
import { ROUTES, useRoute } from './router'
import StockInput from './components/StockInput'
import StockPanel from './components/StockPanel'
import MarketControls from './components/MarketControls'
import AccountSummary from './components/AccountSummary'
import LogPanel from './components/LogPanel'
import PageNav from './components/PageNav'
import UpperLimitPage from './pages/UpperLimitPage'
import BigCandlePage from './pages/BigCandlePage'
import CandleTradePage from './pages/CandleTradePage'
import ZoneTradePage from './pages/ZoneTradePage'
import ManualTradePage from './pages/ManualTradePage'
import BrokerConnection from './components/BrokerConnection'

// 종목 칩의 매매 상태 표시(색은 StateButton과 같은 체계: 대기=파랑, 모니터=주황, 자동=초록).
const STATE_SHORT = { IDLE: '대기', MONITOR: '감시', AUTO_TRADING: '자동' }
const STATE_LABEL = { IDLE: '대기', MONITOR: '모니터', AUTO_TRADING: '자동매매' }

// 한 계좌(모의/실전)의 종목 입력 + 칩바 + 패널 목록. 실전(danger)은 위험 톤.
// [매매]는 상한가 자동매매 전용이다 — 계좌 보유 가져오기·수동 매수·매도는 수동 매매 화면에 있다.
function AccountColumn({ account }) {
  const { stocks, order, hidden, actions } = useStore()
  const acc = account.id
  const accStocks = stocks[acc] || {}
  const accOrder = order[acc] || []
  const accHidden = hidden[acc] || new Set()

  const visibleCodes = accOrder.filter((c) => accStocks[c])
  // 알림(recovery_notice)이 있어도 숨길 수 있다 — 재시작 때마다 보유 종목에 알림이 붙어 토글이
  // 막히던 문제. 대신 칩에 ⚠ 를 달아 알림이 있다는 건 숨겨도 보이게 한다.
  const list = visibleCodes.filter((c) => !accHidden.has(c)).map((c) => accStocks[c])

  return (
    <section className={`account-col ${account.danger ? 'danger' : 'safe'}`}>
      <div className="account-col-head">
        <span className={`account-badge ${account.danger ? 'danger' : 'safe'}`}>
          {account.danger ? '⚠️ 실전' : account.label}
        </span>
        {!account.live && <span className="account-demo">데모 데이터</span>}
        <AccountSummary account={acc} />
      </div>

      <BrokerConnection account={account} />
      {account.recovery_notice && <div className="recovery-notice" role="alert">{account.recovery_notice}</div>}

      <div className="toolbar-row">
        <StockInput account={acc} />
      </div>

      {visibleCodes.length > 0 && (
        <div className="chip-bar">
          {visibleCodes.map((c) => {
            const s = accStocks[c]
            const off = accHidden.has(c)
            const qty = s.position?.quantity || 0
            return (
              <button
                key={c}
                className={`stock-chip ${off ? 'off' : 'on'}`}
                onClick={() => actions.toggleVisible(acc, c)}
                title={`${STATE_LABEL[s.state] || ''} · ${off ? '클릭하면 차트 표시' : '클릭하면 차트 숨김'}`
                  + (s.recovery_notice ? `
⚠️ ${s.recovery_notice}` : '')}
              >
                {s.recovery_notice && <span className="chip-warn" aria-label="알림 있음">⚠</span>}
                {s.state && (
                  <span className={`chip-state chip-state-${s.state.toLowerCase()}`}>
                    {STATE_SHORT[s.state] || s.state}
                  </span>
                )}
                {s.name || c}
                {qty > 0 && <span className="chip-qty" title="계좌 보유 수량">{qty.toLocaleString('ko-KR')}주</span>}
              </button>
            )
          })}
        </div>
      )}

      <div className="panels-col">
        {list.length === 0 && (
          <div className="empty-state">
            {visibleCodes.length === 0
              ? '종목코드를 추가하면 차트와 상한가 자동매매 설정이 나타납니다. 수동 매매는 [수동 매매] 화면에서 합니다.'
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

// 페이지 공통 헤더. 브랜드 + 페이지 전환은 어디서나 같고, 오른쪽 영역만
// 페이지가 채운다(매매 페이지는 계좌 필터 + 장 제어).
function AppHeader({ children }) {
  return (
    <div className="app-top">
      <header className="app-header">
        <div className="head-left">
          <div className="brand">
            <span className="brand-mark">♪</span>
            <span className="brand-name">Bach</span>
            <span className="brand-sub">trading system</span>
          </div>
          <PageNav />
        </div>
        {children}
      </header>
    </div>
  )
}

function TradingPage() {
  const { accounts, accountView } = useStore()
  // 단독 선택 시 그 계좌가 전폭을 쓴다(차트가 넓어짐).
  const shown =
    accountView === 'ALL' ? accounts : accounts.filter((a) => a.id === accountView)
  return (
    <>
      <AppHeader>
        <AccountFilter />
        <MarketControls />
      </AppHeader>

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
    </>
  )
}

// 페이지 전환 시 매매 화면은 언마운트된다(차트 시야는 초기화). WebSocket은
// StoreProvider가 위에 있어 끊기지 않으므로, 돌아와도 상태/시세는 그대로다.
function Router() {
  const route = useRoute()
  const pages = {
    [ROUTES.UPPER_LIMIT]: UpperLimitPage,
    [ROUTES.BIG_CANDLE]: BigCandlePage,
    [ROUTES.CANDLE_TRADE]: CandleTradePage,
    [ROUTES.ZONE_TRADE]: ZoneTradePage,
    [ROUTES.MANUAL_TRADE]: ManualTradePage,
  }
  const Page = pages[route]
  if (Page) {
    return (
      <>
        <AppHeader />
        <Page />
      </>
    )
  }
  return <TradingPage />
}

export default function App() {
  return (
    <StoreProvider>
      <div className="app">
        <Router />
      </div>
    </StoreProvider>
  )
}
