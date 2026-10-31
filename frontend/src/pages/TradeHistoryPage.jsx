// 매매 기록 페이지.
// 기간(기본: 오늘 기준 지난 두 달) 동안 체결이 있었던 종목 목록을 계좌별로 보이고, 종목을 누르면
// 일봉 차트(최근 60거래일)에 그 종목의 매수·매도 체결 지점(B/S 화살표·✕)을 함께 그린다.
// 근거는 계좌별 체결 기록(state.{계좌}.trades.json) — 주문은 나가지 않는다.
// 지금 진행 중인 매매(보유 중·감시 중)는 상태 배지를 붙여 맨 위에 둔다(기간 안 체결이 없어도).
import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useStore } from '../store'
import { api } from '../api'
import Chart from '../components/Chart'

const STRATEGIES = { ulc: '상한가', candle: '대금 양봉', zone: '구간', manual: '수동', bonus: '무상증자' }
const fmt = (n) => Number(n || 0).toLocaleString('ko-KR')

const isoDay = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
// 기본 기간: 오늘부터 두 달 전(말일 보정)까지.
function defaultRange() {
  const end = new Date()
  const start = new Date(end.getFullYear(), end.getMonth() - 2, 1)
  start.setDate(Math.min(end.getDate(), new Date(start.getFullYear(), start.getMonth() + 1, 0).getDate()))
  return { start: isoDay(start), end: isoDay(end) }
}

export default function TradeHistoryPage() {
  const { accounts } = useStore()
  const [account, setAccount] = useState('')
  const [range, setRange] = useState(defaultRange)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState(null)
  const chartRef = useRef(null)
  const acc = accounts.find((a) => a.id === account) || accounts[0]

  const load = useCallback(async (accId, r) => {
    if (!accId) return
    setLoading(true)
    setError('')
    try {
      const res = await api.tradedStocks(accId, r.start, r.end)
      setData({ ...res, account: accId })
      setSelected((cur) => (cur && res.stocks.some((s) => s.code === cur.code) ? cur : null))
    } catch (e) {
      setError(e.message || '매매 기록을 불러오지 못했습니다.')
    } finally {
      setLoading(false)
    }
  }, [])

  // 계좌 목록이 오면(또는 계좌를 바꾸면) 지금 기간으로 조회.
  useEffect(() => { if (acc) load(acc.id, range) }, [acc?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  const onSubmit = (e) => {
    e.preventDefault()
    load(acc?.id, range)
  }
  const resetRange = () => {
    const r = defaultRange()
    setRange(r)
    load(acc?.id, r)
  }
  // 종목을 누르면 애니메이션 없이 바로 차트로 가고, 차트를 닫으면 처음 누르기 전 스크롤 위치로 돌아간다.
  // 그리기 전에(useLayoutEffect) 옮겨 화면이 한 번 튀는 것도 막는다.
  const returnYRef = useRef(null)
  const jumpRef = useRef(false)
  const choose = (s) => {
    if (!selected) returnYRef.current = window.scrollY
    jumpRef.current = true
    setSelected(s)
  }
  const closeChart = () => setSelected(null)
  useLayoutEffect(() => {
    if (selected && jumpRef.current) {
      jumpRef.current = false
      chartRef.current?.scrollIntoView({ behavior: 'instant', block: 'start' })
    } else if (!selected && returnYRef.current !== null) {
      window.scrollTo({ top: returnYRef.current, behavior: 'instant' })
      returnYRef.current = null
    }
  }, [selected])

  const stocks = data && data.account === acc?.id ? data.stocks : []
  const liveCount = stocks.filter((s) => s.status && s.status.kind !== 'setup').length
  const rangeError = range.start && range.end && range.start > range.end ? '시작일이 종료일보다 늦습니다.' : ''

  return (
    <main className="screener trade-history">
      <form className="screener-controls" onSubmit={onSubmit}>
        {accounts.length > 1 && (
          <>
            <label htmlFor="th-account">계좌</label>
            <select id="th-account" value={acc?.id || ''} onChange={(e) => { setAccount(e.target.value); setSelected(null) }}>
              {accounts.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
            </select>
          </>
        )}
        <label htmlFor="th-start">기간</label>
        <input id="th-start" type="date" value={range.start} max={range.end}
          onChange={(e) => setRange((r) => ({ ...r, start: e.target.value }))} />
        <span className="muted">~</span>
        <input id="th-end" type="date" aria-label="종료일" value={range.end} min={range.start}
          onChange={(e) => setRange((r) => ({ ...r, end: e.target.value }))} />
        <button type="submit" className="primary" disabled={loading || !acc || !!rangeError}>
          {loading ? '조회 중…' : '조회'}
        </button>
        <button type="button" className="ghost" disabled={loading || !acc} onClick={resetRange}>지난 두 달</button>
        {rangeError && <span className="down">{rangeError}</span>}
      </form>

      {data && data.account === acc?.id && (
        <div className="screener-summary">
          <span className="sum-main">
            <strong>{data.start}</strong> ~ <strong>{data.end}</strong> 체결 기준
            {accounts.length > 1 && <><span className="sum-sep">·</span>{acc.label}</>}
          </span>
          <span className="sum-count">
            {stocks.length}종목
            {liveCount > 0 && <span className="muted"> (진행 중 {liveCount})</span>}
          </span>
        </div>
      )}

      {error && <div className="screener-error" role="alert"><strong>조회 실패</strong><p>{error}</p></div>}
      {loading && !data && <div className="screener-empty">불러오는 중…</div>}
      {!acc && <div className="screener-empty">계좌 연결을 기다리는 중…</div>}

      {selected && acc && (
        <section className="trade-history-chart" ref={chartRef}>
          <div className="trade-history-chart-head">
            <strong>{selected.name || selected.code}</strong>
            <span className="muted mono">{selected.code}</span>
            <span className="muted">일봉 · 최근 60거래일 · 이 계좌의 매수·매도 체결 표시</span>
            <button type="button" className="ghost" onClick={closeChart}>닫기</button>
          </div>
          <Chart key={`${acc.id}:${selected.code}`} prefKey={`history:${acc.id}:${selected.code}`}
            account={acc.id} code={selected.code} interval={1440} height={420} />
        </section>
      )}

      {data && data.account === acc?.id && stocks.length === 0 && !loading && (
        <div className="screener-empty">이 기간에 매매한 종목이 없습니다.</div>
      )}

      {stocks.length > 0 && (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>종목</th>
                <th>상태</th>
                <th>전략</th>
                <th className="col-num">매수</th>
                <th className="col-num">매도</th>
                <th>첫 체결</th>
                <th>마지막 체결</th>
              </tr>
            </thead>
            <tbody>
              {stocks.map((s) => (
                <tr key={s.code} className={`screener-stock-row${selected?.code === s.code ? ' selected' : ''}`}
                  onClick={() => choose(s)}>
                  <td className="col-name">
                    <button className="screener-stock-link" onClick={(e) => { e.stopPropagation(); choose(s) }}
                      aria-label={`${s.name || s.code} 차트 보기`}>{s.name || s.code}</button>
                    <div className="muted mono">{s.code}</div>
                  </td>
                  <td>{s.status
                    ? <span className={`th-status ${s.status.kind}`}>{s.status.label}</span>
                    : <span className="muted">끝남</span>}</td>
                  <td>{s.strategies.length ? s.strategies.map((k) => STRATEGIES[k] || k).join(' · ') : <span className="muted">—</span>}</td>
                  <td className="col-num">
                    {s.buy_count > 0
                      ? <><span className="up">{fmt(s.buy_qty)}주</span><div className="muted">{s.buy_count}건 · {fmt(s.buy_amount)}원</div></>
                      : <span className="muted">—</span>}
                  </td>
                  <td className="col-num">
                    {s.sell_count > 0
                      ? <><span className="down">{fmt(s.sell_qty)}주</span><div className="muted">{s.sell_count}건 · {fmt(s.sell_amount)}원</div></>
                      : <span className="muted">—</span>}
                  </td>
                  <td>{s.first_day || <span className="muted">—</span>}</td>
                  <td>{s.last_day || <span className="muted">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </main>
  )
}
