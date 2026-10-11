// 구간 매매 페이지.
// 임의 종목에 대해 매입 구간(상단·하단)을 사람이 정하는 자동매매. 대금 양봉 매매와 같은 엔진이라
// 2차 기준·절반 익절·트레일링·구간 이탈 손절이 모두 같고, 다른 점은 구간을 차트에서 수평선으로
// 직접 지정한다는 것뿐이다(예: 대금 양봉 다음 날 10일선 눌림목).
//   종목 추가 → 차트에서 상단·하단 찍기(또는 숫자 입력) → [감시 시작]
//   감시를 시작한 거래일부터 가격이 구간에 들어오면 1차 매수(기한 N거래일)
import React, { useMemo, useState } from 'react'
import { useStore } from '../store'
import CandleTradeCard, { ACTIVE } from '../components/CandleTradeCard'
import TradeChipBar, { useHiddenCards } from '../components/TradeChipBar'

export default function ZoneTradePage() {
  const { accounts, candleTrades } = useStore()
  const rows = useMemo(() => {
    const out = []
    accounts.forEach((a) => {
      Object.values(candleTrades[a.id] || {}).filter((it) => it.kind === 'zone')
        .forEach((it) => out.push({ account: a, item: it }))
    })
    // 운용 중 → 설정 중 → 종료 순, 같은 그룹은 등록 최신순.
    const rank = (p) => (ACTIVE.includes(p) ? 0 : p === 'DRAFT' ? 1 : 2)
    return out.sort((x, y) => rank(x.item.phase) - rank(y.item.phase)
      || String(y.item.created_at).localeCompare(String(x.item.created_at)))
  }, [accounts, candleTrades])
  const { isHidden, toggle } = useHiddenCards('bach.hidden.zoneTrade')
  const shown = rows.filter(({ account, item }) => !isHidden(account, item))

  return (
    <main className="candle-trade">
      <div className="candle-trade-intro">
        <strong>구간 매매</strong>
        <span className="muted">
          아무 종목이나 추가하고, 차트에서 <b>매입 구간의 상단·하단</b>을 수평선으로 찍은 뒤 <b>감시 시작</b>을 누르세요.
          감시를 시작한 거래일부터 가격이 구간에 들어오면 자동으로 1차 매수하고, 2차·익절·손절은 대금 양봉 매매와 같은 규칙을 따릅니다.
        </span>
        <ZoneAddForm accounts={accounts} />
      </div>
      <TradeChipBar rows={rows} isHidden={isHidden} toggle={toggle} showAccount={accounts.length > 1} />
      {rows.length === 0 && (
        <div className="screener-empty">등록된 구간 매매 종목이 없습니다.</div>
      )}
      {rows.length > 0 && shown.length === 0 && (
        <div className="screener-empty">모든 종목이 숨김 상태입니다. 위 종목 배지를 누르면 다시 보입니다.</div>
      )}
      <div className="panels-col">
        {shown.map(({ account, item }) => (
          <CandleTradeCard key={`${account.id}:${item.code}`} account={account} item={item} />
        ))}
      </div>
    </main>
  )
}

function ZoneAddForm({ accounts }) {
  const { actions } = useStore()
  const [account, setAccount] = useState('')
  const [code, setCode] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const acc = accounts.some((a) => a.id === account) ? account : accounts[0]?.id || ''

  const submit = async (e) => {
    e.preventDefault()
    const c = code.trim()
    if (!c || !acc) return
    setBusy(true)
    try {
      await actions.addZoneTrade(acc, c, '')
      setCode('')
      setErr('')
    } catch (e2) {
      setErr(String(e2.message || e2))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="zone-add" onSubmit={submit}>
      {accounts.length > 1 && (
        <select value={acc} onChange={(e) => setAccount(e.target.value)} aria-label="계좌">
          {accounts.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
        </select>
      )}
      <input placeholder="종목코드 (예: 002990)" value={code} onChange={(e) => setCode(e.target.value)}
        aria-label="종목코드" />
      <button type="submit" className="add-btn" disabled={busy || !code.trim()}>+ 구간 매매 추가</button>
      {err && <span className="input-err">{err}</span>}
    </form>
  )
}
