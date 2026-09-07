// 상한가 종목 페이지.
// 지정일 D의 종가가 직전 거래일 종가 대비 +29~30%인 종목을 표로 보여준다.
// D-1은 달력상 전날이 아니라 '직전 거래일'이며, 어느 날이 기준이 됐는지는
// 서버가 prev_date로 돌려주므로 그대로 표시한다(휴장일을 건너뛴 게 보이도록).
import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../api'

// 시가총액은 원 단위 그대로 보면 자릿수를 셀 수 없다. 조/억으로 접는다.
function formatMarketCap(won) {
  if (!won || won <= 0) return '—'
  const jo = Math.floor(won / 1e12)
  const eok = Math.floor((won % 1e12) / 1e8)
  if (jo > 0) return `${jo.toLocaleString()}조 ${eok.toLocaleString()}억`
  if (eok > 0) return `${eok.toLocaleString()}억`
  return `${Math.round(won / 1e4).toLocaleString()}만`
}

const won = (n) => Number(n || 0).toLocaleString()

export default function UpperLimitPage() {
  const [date, setDate] = useState('')       // '' = 가장 최근 거래일(서버가 결정)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const load = useCallback(async (d) => {
    setLoading(true)
    setError(null)
    try {
      const res = await api.upperLimit(d)
      setData(res)
      setDate(res.date)   // 서버가 고른 거래일을 입력칸에 반영
    } catch (e) {
      setData(null)
      setError({ message: String(e.message || e), status: e.status })
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load('') }, [load])

  const onSubmit = (e) => {
    e.preventDefault()
    load(date)
  }

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
            {data.stocks.length}종목
            {!data.snapshot && <span className="muted"> / {won(data.scanned)}종목 조회</span>}
          </span>
          {data.source === 'kiwoom' && (
            <span className="live-badge" title="키움 ka10017 최근 정규장 시세입니다">
              키움 시세
            </span>
          )}
          {data.source === 'mock' && (
            <span className="demo-badge" title="KRX_OPEN_API_KEY 미설정 — 합성 데이터입니다">
              데모 데이터
            </span>
          )}
        </div>
      )}

      {error && (
        <div className="screener-error" role="alert">
          <strong>조회 실패</strong>
          <p>{error.message}</p>
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

      {data && data.stocks.length === 0 && !loading && (
        <div className="screener-empty">
          {data.date}에는 상한가 조건을 만족하는 종목이 없습니다.
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
                <th className="col-num">시가총액</th>
                <th className="col-num" title={data.snapshot ? '현재까지 누적 거래량' : '조회일 거래량'}>
                  거래량{data.snapshot ? ' (누적)' : ''}
                </th>
                <th className="col-num">{data.snapshot ? '현재가' : '종가'}</th>
                <th className="col-num">등락률</th>
              </tr>
            </thead>
            <tbody>
              {data.stocks.map((s, i) => (
                <tr key={s.code}>
                  <td className="col-num muted">{i + 1}</td>
                  <td className="mono">{s.code}</td>
                  <td className="col-name">{s.name || '—'}</td>
                  <td>
                    <span className={`mkt mkt-${s.market.toLowerCase()}`}>{s.market}</span>
                  </td>
                  <td className="col-num">{formatMarketCap(s.market_cap)}</td>
                  <td className="col-num">{won(s.volume)}</td>
                  <td className="col-num strong">{won(s.close)}</td>
                  <td className="col-num up" title={`직전 거래일 종가 ${won(s.prev_close)}원`}>
                    +{s.change_pct.toFixed(2)}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </main>
  )
}
