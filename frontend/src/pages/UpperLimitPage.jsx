// 상한가 종목 페이지.
// 지정일 D의 종가가 직전 거래일 종가 대비 +29~30%인 종목을 표로 보여준다.
// D-1은 달력상 전날이 아니라 '직전 거래일'이며, 어느 날이 기준이 됐는지는
// 서버가 prev_date로 돌려주므로 그대로 표시한다(휴장일을 건너뛴 게 보이도록).
import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import ScreenerChart from '../components/ScreenerChart'
import SortHeader, { sortRows, useSort } from '../components/SortHeader'
import { ROUTES, navigate } from '../router'

// 시가총액·거래대금은 원 단위 그대로 보면 자릿수를 셀 수 없다. 조/억으로 접는다.
function formatEok(won) {
  if (!won || won <= 0) return '—'
  const jo = Math.floor(won / 1e12)
  const eok = Math.floor((won % 1e12) / 1e8)
  if (jo > 0) return `${jo.toLocaleString()}조 ${eok.toLocaleString()}억`
  if (eok > 0) return `${eok.toLocaleString()}억`
  return `${Math.round(won / 1e4).toLocaleString()}만`
}

const won = (n) => Number(n || 0).toLocaleString()
const signedPct = (v) => `${v > 0 ? '+' : ''}${v.toFixed(2)}%`

// 큰 값이 위로 오는 정렬 기준. 동률이면 다른 기준으로 한 번 더 가른다.
const SORT_KEYS = {
  volume: ['volume', 'market_cap'],
  amount: ['amount', 'volume'],
  market_cap: ['market_cap', 'volume'],
}

export default function UpperLimitPage() {
  const [date, setDate] = useState('')       // '' = 가장 최근 거래일(서버가 결정)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selectedStock, setSelectedStock] = useState(null)
  const [sort, onSort] = useSort('volume')
  // 정규장 상한가·시간외 이탈 종목은 본 목록과 따로 보여준다.
  const stocks = useMemo(() => (data ? sortRows(data.stocks.filter((s) => !s.after_hours_drop), SORT_KEYS[sort.key], sort.dir) : []), [data, sort])
  const drops = useMemo(() => (data ? sortRows(data.stocks.filter((s) => s.after_hours_drop), SORT_KEYS[sort.key], sort.dir) : []), [data, sort])

  const load = useCallback(async (d) => {
    setLoading(true)
    setSelectedStock(null)
    setError(null)
    try {
      const res = await api.upperLimit(d)
      setData(res)
      setDate(res.date)   // 서버가 고른 거래일을 입력칸에 반영
    } catch (e) {
      setData(null)
      setError({ message: String(e.message || e), status: e.status,
        pending: e.code === 'DATA_PENDING', availableAfter: e.availableAfter })
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load('') }, [load])

  const onSubmit = (e) => {
    e.preventDefault()
    load(date)
  }

  const priceLabel = data?.cached ? '저장 시점 가격' : data?.snapshot ? '현재가' : '종가'
  const renderTable = (rows) => (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th className="col-num">#</th>
            <th>종목코드</th>
            <th>종목명</th>
            <th>시장</th>
            <SortHeader sortKey="market_cap" sort={sort} onSort={onSort} title="시가총액">
              시가총액
            </SortHeader>
            <SortHeader sortKey="volume" sort={sort} onSort={onSort}
              title={`${data.snapshot ? '현재까지 누적 거래량' : '조회일 거래량'}`}>
              거래량{data.snapshot ? ' (누적)' : ''}
            </SortHeader>
            <SortHeader sortKey="amount" sort={sort} onSort={onSort}
              title={`${data.snapshot ? '현재까지 누적 거래대금' : '조회일 거래대금'}`}>
              거래대금
            </SortHeader>
            <th className="col-num">{priceLabel}</th>
            <th className="col-num">등락률</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s, i) => (
            <tr key={s.code} className="screener-stock-row" onClick={() => setSelectedStock(s)}>
              <td className="col-num muted">{i + 1}</td>
              <td className="mono">{s.code}</td>
              <td className="col-name">
                <button className="screener-stock-link" onClick={(e) => { e.stopPropagation(); setSelectedStock(s) }} aria-label={`${s.name || s.code} 차트 보기`}>{s.name || '—'}</button>
                {s.after_hours && (
                  <span className="after-hours-badge" title="정규장 종가는 상한가 미만이었고, 15:30 이후 시간외(KRX·NXT)에서 상한가에 도달했습니다">시간외</span>
                )}
                {s.after_hours_drop && (
                  <span className="after-hours-badge drop" title="정규장은 상한가로 마감했지만 시간외(KRX·NXT)에서 밀렸습니다">시간외 이탈</span>
                )}
              </td>
              <td>
                <span className={`mkt mkt-${s.market.toLowerCase()}`}>{s.market}</span>
              </td>
              <td className="col-num">{formatEok(s.market_cap)}</td>
              <td className="col-num">{won(s.volume)}</td>
              <td className="col-num">{formatEok(s.amount)}</td>
              <td className="col-num strong">{won(s.close)}</td>
              <td className={`col-num ${s.change_pct >= 0 ? 'up' : 'down'}`} title={`직전 거래일 종가 ${won(s.prev_close)}원`}>
                {signedPct(s.change_pct)}
                {(s.after_hours || s.after_hours_drop) && s.regular_close > 0 && s.prev_close > 0 && (
                  <div className="regular-close muted">
                    정규장 {won(s.regular_close)} ({signedPct((s.regular_close - s.prev_close) / s.prev_close * 100)})
                  </div>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )

  return (
    <main className="screener">
      <form className="screener-controls" onSubmit={onSubmit}>
        <label htmlFor="ul-date">조회일</label>
        <input
          id="ul-date"
          type="date"
          value={date}
          onChange={(e) => setDate(e.target.value)}
        />
        <button type="submit" className="primary" disabled={loading}>
          {loading ? '조회 중…' : '조회'}
        </button>
        <button
          type="button"
          className="ghost"
          disabled={loading}
          onClick={() => load('')}
        >
          최근 거래일
        </button>
        <span className="screener-criteria">
          직전 거래일 종가 대비 <strong>+29% ~ +30%</strong>
        </span>
      </form>

      {data && (
        <div className="screener-summary">
          <span className="sum-main">
            <strong>{data.date}</strong> {data.snapshot ? '키움 시세 기준' : '종가 기준'}
            <span className="sum-sep">·</span>
            직전 거래일 <strong>{data.prev_date}</strong> 대비
          </span>
          <span className="sum-count">
            {stocks.length}종목
            {drops.length > 0 && <span className="muted"> (+ 시간외 이탈 {drops.length})</span>}
            {!data.snapshot && <span className="muted"> / {won(data.scanned)}종목 조회</span>}
          </span>
          {data.source === 'kiwoom' && (
            <span className="live-badge" title="키움 ka10017 최근 정규장 시세입니다">
              {data.cached ? '저장된 키움 시세' : '키움 시세'}
            </span>
          )}
          {data.source === 'mock' && (
            <span className="demo-badge" title="KRX_OPEN_API_KEY 미설정 — 합성 데이터입니다">
              데모 데이터
            </span>
          )}
        </div>
      )}

      {data?.notice && <div className="screener-notice" role="status">{data.notice}</div>}

      {error && (
        <div className={error.pending ? 'screener-notice' : 'screener-error'} role={error.pending ? 'status' : 'alert'}>
          <strong>{error.pending ? 'KRX 자료 게시 대기' : '조회 실패'}</strong>
          <p>{error.message}</p>
          {error.pending && <button className="ghost" disabled={loading} onClick={() => load(date)}>다시 조회</button>}
          {error.status === 503 && (
            <p className="muted">
              실데이터를 보려면 <code>openapi.krx.co.kr</code>에서 인증키를 발급받고
              ‘유가증권/코스닥 일별매매정보’ 이용신청을 마친 뒤,
              <code>backend/.env</code>에 <code>KRX_OPEN_API_KEY</code>를 설정하세요.
              키 없이 화면만 보려면 <code>SCREENER_SOURCE=mock</code>으로 띄웁니다.
            </p>
          )}
        </div>
      )}

      {loading && !data && <div className="screener-empty">불러오는 중…</div>}

      {data && stocks.length === 0 && !loading && (
        <div className="screener-empty">
          {data.date}에는 상한가 조건을 만족하는 종목이 없습니다.
        </div>
      )}

      {data && stocks.length > 0 && renderTable(stocks)}

      {data && drops.length > 0 && (
        <section className="after-hours-drops" aria-labelledby="ul-drops-title">
          <h3 id="ul-drops-title">
            정규장 상한가 · 시간외 이탈 <span className="muted">{drops.length}종목</span>
          </h3>
          <p className="muted">
            정규장(15:30)은 상한가로 마감했지만 시간외(KRX·NXT)에서 밀려 마지막 거래가가 상한가 구간 밖인 종목입니다.
            가격·등락률은 마지막 거래가 기준이며, ULC의 전일 종가(X)도 이 값입니다.
          </p>
          {renderTable(drops)}
        </section>
      )}
      {selectedStock && (
        <ScreenerChart
          stock={selectedStock}
          source={data.source}
          register={{
            label: '매매 등록',
            title: (account) => `${account.label} 계좌의 [매매] 목록에 수동매매로 추가합니다(주문은 나가지 않음)`,
            run: async (account) => {
              try {
                await api.addStock(account.id, selectedStock.code, selectedStock.name)
                navigate(ROUTES.TRADING)
              } catch (e) {
                window.alert(`매매 등록 실패: ${e.message || e}`)
              }
            },
          }}
          onClose={() => setSelectedStock(null)}
        />
      )}
    </main>
  )
}
