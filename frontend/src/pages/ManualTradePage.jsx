// 수동 매매 페이지.
// 어느 자동매매([상한가 매매] · 대금 양봉 매매 · 구간 매매)에서든 [수동 전환]한 종목이 여기로 모이고,
// 아무 종목이나 추가해 처음부터 직접 사고팔 수도 있다. 엔진이 없으므로 보유·평단은 계좌 잔고가 기준이다.
//   전환은 되돌리지 않는다 — 다시 자동매매하려면 보유 0에서 지우고 해당 화면에 새로 추가
//   자동 손절·익절(평단 대비 % 또는 가격 라인, 전량 시장가, 주문 전송 후 둘 다 해제)은 종목별로 켠다.
//   차트에 평단·손절·익절 선을 그리고, 손절·익절 선은 끌어서 옮긴다. [측정]으로 두 점의 변화율을 본다
//   재시작·[보유 종목 가져오기]: 계좌 보유 중 어느 목록에도 없는 종목도 여기로 들어온다
import React, { useMemo, useState } from 'react'
import { useStore } from '../store'
import Chart from '../components/Chart'
import IntervalSelector from '../components/IntervalSelector'
import PriceTicker from '../components/PriceTicker'
import ManualTradePanel from '../components/ManualTradePanel'
import ManualExits, { useExitDraft, useExitLines } from '../components/ManualExits'
import { roundToTick } from '../candleZone'
import { useHiddenCards } from '../components/TradeChipBar'
import AccountViewFilter, { useAccountView } from '../components/AccountViewFilter'

const ORIGINS = {
  added: '직접 추가',
  ulc: '상한가 매매에서',
  candle: '대금 양봉 매매에서',
  zone: '구간 매매에서',
  restore: '재시작 잔고',
  import: '보유 가져오기',
}
const fmt = (n) => Number(n || 0).toLocaleString('ko-KR')
const held = (it) => (it.position?.quantity || 0) > 0

export default function ManualTradePage() {
  const { accounts, manualTrades } = useStore()
  const rows = useMemo(() => {
    const out = []
    accounts.forEach((a) => {
      Object.values(manualTrades[a.id] || {}).forEach((it) => out.push({ account: a, item: it }))
    })
    // 보유 중 → 보유 없음 순, 같은 그룹은 옮겨 온 최신순.
    return out.sort((x, y) => Number(held(y.item)) - Number(held(x.item))
      || String(y.item.created_at).localeCompare(String(x.item.created_at)))
  }, [accounts, manualTrades])
  const { isHidden, toggle } = useHiddenCards('bach.hidden.manualTrade')
  const { view, choose, visible } = useAccountView('bach.accountView.manualTrade', accounts)
  const inView = rows.filter(({ account }) => visible(account))
  const shown = inView.filter(({ account, item }) => !isHidden(account, item))

  return (
    <main className="candle-trade">
      <div className="candle-trade-intro">
        <strong>수동 매매</strong>
        <span className="muted">
          어느 자동매매에서든 <b>수동 전환</b>한 종목이 여기로 옮겨옵니다. 아무 종목이나 추가해 직접 사고팔 수도 있습니다.
          자동 주문은 없고(켜 둔 자동 손절만 예외), 보유·평단은 계좌 잔고 기준입니다.
          다시 자동매매하려면 보유 0에서 지운 뒤 해당 화면에 새로 추가하세요.
        </span>
        <ManualAddForm accounts={accounts} />
      </div>
      <AccountViewFilter accounts={accounts} rows={rows} view={view} choose={choose} />
      <ManualChipBar rows={inView} isHidden={isHidden} toggle={toggle} showAccount={accounts.length > 1} />
      {rows.length === 0 && (
        <div className="screener-empty">수동 매매 중인 종목이 없습니다.</div>
      )}
      {rows.length > 0 && inView.length === 0 && (
        <div className="screener-empty">이 계좌에는 종목이 없습니다.</div>
      )}
      {inView.length > 0 && shown.length === 0 && (
        <div className="screener-empty">모든 종목이 숨김 상태입니다. 위 종목 배지를 누르면 다시 보입니다.</div>
      )}
      <div className="panels-col">
        {shown.map(({ account, item }) => (
          <ManualTradeCard key={`${account.id}:${item.code}`} account={account} item={item} />
        ))}
      </div>
    </main>
  )
}

function ManualAddForm({ accounts }) {
  const { actions } = useStore()
  const [account, setAccount] = useState('')
  const [code, setCode] = useState('')
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const acc = accounts.some((a) => a.id === account) ? account : accounts[0]?.id || ''

  const submit = async (e) => {
    e.preventDefault()
    const c = code.trim()
    if (!c || !acc) return
    setBusy(true)
    try {
      await actions.addManual(acc, c, '')
      setCode('')
      setMsg('')
    } catch (e2) {
      setMsg(String(e2.message || e2))
    } finally {
      setBusy(false)
    }
  }
  const importHeld = async () => {
    if (!acc) return
    setBusy(true)
    try {
      const added = await actions.importHeld(acc)
      setMsg(added.length ? `${added.length}개 가져옴` : '가져올 보유 종목 없음(다른 목록에 있는 종목은 제외)')
    } catch (e2) {
      setMsg(String(e2.message || e2))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="zone-add" onSubmit={submit}>
      {/* 새 종목을 넣을 계좌. 아래 목록의 필터가 아니다(목록은 '계좌 보기'로 거른다). */}
      {accounts.length > 1 && (
        <label className={`zone-add-acct${accounts.find((a) => a.id === acc)?.danger ? ' danger' : ''}`}>
          추가할 계좌
          <select value={acc} onChange={(e) => setAccount(e.target.value)}>
            {accounts.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
          </select>
        </label>
      )}
      <input placeholder="종목코드 (예: 005930)" value={code} onChange={(e) => setCode(e.target.value)}
        aria-label="종목코드" />
      <button type="submit" className="add-btn" disabled={busy || !code.trim()}>+ 수동 매매 추가</button>
      <button type="button" className="import-held-btn" disabled={busy || !acc} onClick={importHeld}
        title="계좌 보유 종목 중 어느 목록에도 없는 것을 가져옵니다">
        보유 종목 가져오기
      </button>
      {msg && <span className="input-err">{msg}</span>}
    </form>
  )
}

function ManualChipBar({ rows, isHidden, toggle, showAccount }) {
  if (rows.length === 0) return null
  return (
    <div className="chip-bar trade-chip-bar">
      {rows.map(({ account, item }) => {
        const off = isHidden(account, item)
        const qty = item.position?.quantity || 0
        return (
          <button
            key={`${account.id}:${item.code}`}
            className={`stock-chip ${off ? 'off' : 'on'}`}
            onClick={() => toggle(account, item)}
            aria-pressed={!off}
            title={`${showAccount ? `${account.label} · ` : ''}${ORIGINS[item.origin] || item.origin} · `
              + `${off ? '클릭하면 카드 표시' : '클릭하면 카드 숨김'}` + (item.notice ? `
⚠️ ${item.notice}` : '')}
          >
            {item.notice && <span className="chip-warn" aria-label="알림 있음">⚠</span>}
            <span className="chip-state chip-state-manual">수동</span>
            {showAccount && <span className="chip-acc">{account.label}</span>}
            {item.name || item.code}
            {qty > 0 && <span className="chip-qty" title="계좌 보유 수량">{fmt(qty)}주</span>}
          </button>
        )
      })}
    </div>
  )
}

function ManualTradeCard({ account, item }) {
  const { ticks, actions } = useStore()
  const [interval, setInterval] = useState(1440) // 기본 일봉
  const tick = (ticks[account.id] || {})[item.code]
  const verified = item.position_verified !== false
  const exitDraft = useExitDraft(item)
  const avg = verified ? Number(item.position?.avg_price || 0) : 0
  const { lines, drag } = useExitLines(exitDraft.draft, avg)
  // 선을 끌면 그 가격(호가 단위)의 라인 방식으로 바꾼다. 저장은 폼에서.
  // 그 선을 긋는 중이었으면 끌어서 놓은 것으로 긋기를 마친다(뒤이은 클릭이 선을 다시 옮기지 않게).
  const onLineDrag = (side, p) => {
    exitDraft.update(side, { mode: 'price', price: roundToTick(p), enabled: true })
    if (exitDraft.pick === side) exitDraft.setPick(null)
  }

  const remove = async () => {
    if (!window.confirm(`${item.code} 를 수동 매매 목록에서 지울까요? (계좌 보유가 0일 때만 지워집니다)`)) return
    try {
      await actions.removeManual(account.id, item.code)
    } catch (e) {
      window.alert(String(e.message || e))
    }
  }

  return (
    <div className={`stock-panel manual-card ${account.danger ? 'danger' : 'safe'}`}>
      <div className="panel-header">
        <div className="panel-id">
          <span className="panel-code">{item.code}</span>
          <span className="panel-name">{item.name}</span>
          <span className={`account-badge ${account.danger ? 'danger' : 'safe'}`}>
            {account.danger ? '⚠️ 실전' : account.label}
          </span>
          <span className="origin-badge" title={item.created_at ? `옮겨온 시각 ${item.created_at}` : ''}>
            {ORIGINS[item.origin] || item.origin}
          </span>
        </div>
        <PriceTicker tick={tick} />
        <div className="panel-actions">
          <button className="remove-btn" onClick={remove} title="목록에서 지우기(보유 0일 때)">✕</button>
        </div>
      </div>

      {item.notice && (
        <div className="recovery-notice" role="alert">
          {item.notice}
          <button className="link-btn notice-ok" onClick={() => actions.clearManualNotice(account.id, item.code)}>확인</button>
        </div>
      )}
      {!verified && <div className="recovery-notice" role="alert">잔고 조회 실패 — 표시 수량을 신뢰하지 말고 계좌에서 확인하세요.</div>}

      <div className="chart-toolbar">
        <IntervalSelector value={interval} onChange={setInterval} />
        {exitDraft.pick && (
          <span className={`draw-hint draw-${exitDraft.pick}`}>
            차트를 눌러 {exitDraft.pick === 'stop' ? '손절' : '익절'} 라인을 그으세요
            <button type="button" className="link-btn" onClick={() => exitDraft.setPick(null)}>취소</button>
          </span>
        )}
      </div>
      <Chart account={account.id} code={item.code} interval={interval} tick={tick} height={300}
        priceLines={lines} draggableLines={drag} onLineDrag={onLineDrag}
        onPriceClick={exitDraft.pick ? exitDraft.drawAt : null} />
      <div className="panels manual-panels">
        <ManualTradePanel account={account.id} code={item.code} position={item.position}
          verified={verified} tick={tick} />
        <ManualExits account={account.id} code={item.code} position={item.position}
          verified={verified} tick={tick} exitDraft={exitDraft} />
      </div>
    </div>
  )
}
