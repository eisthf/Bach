// 거래대금 상위 양봉 페이지.
// 조회 시점의 거래대금이 기준(기본 150억) 이상이면서 양봉(현재가/종가 > 시가)이고,
// 시가 대비 상승률이 지정값 이상인 종목을 표로 보여준다.
// - 오늘 장중: 키움 당일 시세의 '현재가' 기준 (조회 시각의 스냅샷)
// - 오늘 장 마감 후: 키움 당일 시세의 '종가' 기준
// - 과거일: KRX 확정 일별 자료의 종가 기준
// 종목을 누르면 상한가 페이지와 같은 차트 대화상자를 띄운다(일봉이 기본).
import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import ScreenerChart from '../components/ScreenerChart'
import { ROUTES, navigate } from '../router'

const RISE_KEY = 'bach.bigCandle.minRise'
const AMOUNT_KEY = 'bach.bigCandle.minAmountEok'

const loadNum = (key, fallback) => {
  try {
    const v = Number(localStorage.getItem(key))
    return localStorage.getItem(key) !== null && Number.isFinite(v) ? v : fallback
  } catch { return fallback }
}
const saveNum = (key, value) => {
  try { localStorage.setItem(key, String(value)) } catch { /* 저장 불가 환경 무시 */ }
}

const won = (n) => Number(n || 0).toLocaleString()

// 거래대금·시가총액은 원 단위로는 읽기 어렵다. 조/억으로 접는다.
function formatEok(won) {
  if (!won || won <= 0) return '—'
  const jo = Math.floor(won / 1e12)
  const eok = Math.round((won % 1e12) / 1e8)
  if (jo > 0) return `${jo.toLocaleString()}조 ${eok.toLocaleString()}억`
  return `${eok.toLocaleString()}억`
}

const signedPct = (v) => (v === null || v === undefined ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`)
const pctClass = (v) => (v > 0 ? 'up' : v < 0 ? 'down' : '')

const timeOf = (iso) => (iso ? iso.slice(11, 16) : '')

export default function BigCandlePage() {
  const [date, setDate] = useState('')        // '' = 가장 최근 거래일(서버가 결정)
  const [minRise, setMinRise] = useState(() => loadNum(RISE_KEY, 0))
  const [minAmount, setMinAmount] = useState(() => loadNum(AMOUNT_KEY, 150))
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selectedStock, setSelectedStock] = useState(null)

  const load = useCallback(async (d, rise, amount) => {
    setLoading(true)
    setSelectedStock(null)
    setError(null)
    try {
      const res = await api.bigCandle(d, rise, amount)
      setData(res)
      setDate(res.date)   // 서버가 고른 거래일을 입력칸에 반영
    } catch (e) {
      setData(null)
      setError({ message: String(e.message || e), status: e.status,
        pending: e.code === 'DATA_PENDING' })
    } finally {
      setLoading(false)
    }
  }, [])

  // 최초 1회만 저장된 조건으로 조회한다(입력 중 값 변경마다 조회하지 않음).
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load('', minRise, minAmount) }, [load])

  const submit = (d) => {
    const rise = Math.max(0, Number(minRise) || 0)
    const amount = Math.max(1, Math.round(Number(minAmount) || 150))
    setMinRise(rise)
    setMinAmount(amount)
    saveNum(RISE_KEY, rise)
    saveNum(AMOUNT_KEY, amount)
    load(d, rise, amount)
  }

  const onSubmit = (e) => {
    e.preventDefault()
    submit(date)
  }

  const live = data?.snapshot && !data?.closed   // 장중 현재가 기준
  const priceLabel = live ? '현재가' : '종가'

  return (
    <main className="screener">
      <form className="screener-controls" onSubmit={onSubmit}>
        <label htmlFor="bc-date">조회일</label>
        <input id="bc-date" type="date" value={date} onChange={(e) => setDate(e.target.value)} />
        <label htmlFor="bc-rise">시가 대비</label>
        <input
          id="bc-rise"
          className="num-input"
          type="number"
          min="0"
          max="100"
          step="any"
          value={minRise}
          onChange={(e) => setMinRise(e.target.value)}
          aria-describedby="bc-rise-unit"
        />
        <span id="bc-rise-unit">% 이상</span>
        <label htmlFor="bc-amount">거래대금</label>
        <input
          id="bc-amount"
          className="num-input"
          type="number"
          min="1"
          step="1"
          value={minAmount}
          onChange={(e) => setMinAmount(e.target.value)}
          aria-describedby="bc-amount-unit"
        />
        <span id="bc-amount-unit">억 이상</span>
        <button type="submit" className="primary" disabled={loading}>
          {loading ? '조회 중…' : '조회'}
        </button>
        <button type="button" className="ghost" disabled={loading} onClick={() => submit('')}>
          최근 거래일
        </button>
      </form>

      {data && (
        <div className="screener-summary">
          <span className="sum-main">
            <strong>{data.date}</strong>{' '}
            {data.snapshot
              ? (live ? <>{timeOf(data.captured_at)} 조회 · <strong>현재가</strong> 기준</> : <>장 마감 · <strong>종가</strong> 기준</>)
              : <>확정 <strong>종가</strong> 기준</>}
            <span className="sum-sep">·</span>
            거래대금 <strong>{formatEok(data.min_amount)}</strong> 이상 양봉,
            시가 대비 <strong>+{Number(data.min_rise_pct).toFixed(2)}%</strong> 이상
          </span>
          <span className="sum-count">
            {data.stocks.length}종목
            <span className="muted"> / 거래대금 기준 {won(data.scanned)}종목</span>
          </span>
          {data.source === 'kiwoom' && (
            <span className="live-badge" title="키움 ka10032·ka10028 당일 시세(KRX)입니다">키움 시세</span>
          )}
          {data.source === 'mock' && (
            <span className="demo-badge" title="KRX_OPEN_API_KEY 미설정 — 합성 데이터입니다">데모 데이터</span>
          )}
        </div>
      )}

      {data?.notice && <div className="screener-notice" role="status">{data.notice}</div>}

      {error && (
        <div className={error.pending ? 'screener-notice' : 'screener-error'} role={error.pending ? 'status' : 'alert'}>
          <strong>{error.pending ? 'KRX 자료 게시 대기' : '조회 실패'}</strong>
          <p>{error.message}</p>
          <button className="ghost" disabled={loading} onClick={() => submit(date)}>다시 조회</button>
        </div>
      )}

      {loading && !data && <div className="screener-empty">불러오는 중…</div>}

      {data && data.stocks.length === 0 && !loading && (
        <div className="screener-empty">
          {data.date}에는 조건을 만족하는 종목이 없습니다.
        </div>
      )}

      {data && data.stocks.length > 0 && (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th className="col-num">#</th>
                <th>종목코드</th>
                <th>종목명</th>
                <th>시장</th>
                <th className="col-num" title={live ? '현재까지 누적 거래대금' : '당일 거래대금'}>
                  거래대금{live ? ' (누적)' : ''}
                </th>
                <th className="col-num">시가</th>
                <th className="col-num">{priceLabel}</th>
                <th className="col-num" title={`${priceLabel} ÷ 시가 − 1`}>시가 대비</th>
                <th className="col-num" title="직전 거래일 종가 대비">전일 대비</th>
              </tr>
            </thead>
            <tbody>
              {data.stocks.map((s, i) => (
                <tr key={s.code} className="screener-stock-row" onClick={() => setSelectedStock(s)}>
                  <td className="col-num muted">{i + 1}</td>
                  <td className="mono">{s.code}</td>
                  <td className="col-name">
                    <button
                      className="screener-stock-link"
                      onClick={(e) => { e.stopPropagation(); setSelectedStock(s) }}
                      aria-label={`${s.name || s.code} 차트 보기`}
                    >
                      {s.name || '—'}
                    </button>
                  </td>
                  <td>
                    {s.market
                      ? <span className={`mkt mkt-${s.market.toLowerCase()}`}>{s.market}</span>
                      : <span className="muted">—</span>}
                  </td>
                  <td className="col-num">{formatEok(s.amount)}</td>
                  <td className="col-num">{won(s.open)}</td>
                  <td className="col-num strong">{won(s.close)}</td>
                  <td className="col-num up">+{s.rise_pct.toFixed(2)}%</td>
                  <td className={`col-num ${pctClass(s.change_pct)}`}>{signedPct(s.change_pct)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {selectedStock && (
        <ScreenerChart
          stock={selectedStock}
          source={data.source === 'mock' ? 'mock' : 'kiwoom'}
          includeToday={live}
          defaultInterval={1440}
          onRegister={async (account) => {
            try {
              await api.addCloseTrade(account.id, selectedStock.code, selectedStock.name)
              navigate(`${ROUTES.CLOSE_TRADE}`)
            } catch (e) {
              window.alert(`종가 매매 등록 실패: ${e.message || e}`)
            }
          }}
          onClose={() => setSelectedStock(null)}
        />
      )}
    </main>
  )
}
