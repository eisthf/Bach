// 거래대금 상위 양봉 페이지.
// 조회 시점의 거래대금이 기준(기본 150억) 이상이면서 양봉(현재가/종가 > 시가)이고,
// 시가 대비 상승률과 전일 대비 등락률이 각각 지정값 이상인 종목을 표로 보여준다.
// - 오늘 장중: 키움 당일 시세의 '현재가' 기준 (조회 시각의 스냅샷)
// - 오늘 장 마감 후: 키움 당일 시세의 '종가' 기준
// - 과거일: KRX 확정 일별 자료의 종가 기준
// 종목을 누르면 상한가 페이지와 같은 차트 대화상자를 띄운다(일봉이 기본).
import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import ScreenerChart from '../components/ScreenerChart'
import SortHeader, { sortRows, useSort } from '../components/SortHeader'
import { ROUTES, navigate } from '../router'
import { DEFAULT_CANDLE_CONFIG, addRuleText, secondLegCheck, zoneOf } from '../candleZone'
import { ANCHOR_COLOR } from '../tradeMarkers'

const RISE_KEY = 'bach.bigCandle.minRise'
const CHANGE_KEY = 'bach.bigCandle.minChange'
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

// 큰 값이 위로 오는 정렬 기준. 동률이면 뒤 키로 한 번 더 가른다.
const SORT_KEYS = {
  volume: ['volume', 'market_cap'],
  market_cap: ['market_cap', 'volume'],
  amount: ['amount', 'volume'],
}

export default function BigCandlePage() {
  const [date, setDate] = useState('')        // '' = 가장 최근 거래일(서버가 결정)
  const [minRise, setMinRise] = useState(() => loadNum(RISE_KEY, 0))
  const [minChange, setMinChange] = useState(() => loadNum(CHANGE_KEY, 0))
  const [minAmount, setMinAmount] = useState(() => loadNum(AMOUNT_KEY, 150))
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selectedStock, setSelectedStock] = useState(null)
  const [sort, onSort] = useSort('amount')
  // 종가(장중이면 현재가)가 그날 고가인 종목만 — 고가에서 마감한 강한 양봉. 화면 필터.
  // 고가를 모르는(0) 종목은 판정할 수 없어 켜면 빠진다.
  const [closeAtHigh, setCloseAtHigh] = useState(false)
  // 과거일 목록(KRX 확정 자료)의 고가·종가는 정규장만이다. 기본은 키움 일봉(15:30 이후 시간외
  // 체결 포함 — 종목 차트가 보여 주는 값)으로 한 번 더 확인해 시간외 위꼬리가 있으면 뺀다.
  // allowAfterWick 를 켜면 정규장 고가 마감이면 통과(시간외 위꼬리 허용).
  const [allowAfterWick, setAllowAfterWick] = useState(false)
  const [dayBars, setDayBars] = useState(null)   // {key, available, bars, failed} | {key, loading} | {key, error}
  const needDayBars = closeAtHigh && !allowAfterWick && data?.source === 'krx'
  // 정규장 고가 마감 후보만 묻는다. 조건을 바꿔 다시 조회하면 후보가 달라지므로 날짜+후보로 구분.
  const wickCodes = useMemo(() => (data?.source === 'krx'
    ? data.stocks.filter((s) => !s.etp && s.high > 0 && s.close >= s.high).map((s) => s.code).slice(0, 80) : []), [data])
  const wickKey = data ? `${data.date}|${wickCodes.join(',')}` : ''
  useEffect(() => {
    if (!needDayBars || dayBars?.key === wickKey) return undefined
    if (wickCodes.length === 0) { setDayBars({ key: wickKey, available: true, bars: {}, failed: [] }); return undefined }
    let cancelled = false
    setDayBars({ key: wickKey, loading: true })
    api.bigCandleDayBars(data.date, wickCodes)
      .then((r) => { if (!cancelled) setDayBars({ ...r, key: wickKey }) })
      .catch((e) => { if (!cancelled) setDayBars({ key: wickKey, error: String(e.message || e) }) })
    // 응답 전에 끄거나 조건이 바뀌면 '확인 중'에 멈추지 않게 지운다(다시 켜면 새로 묻는다).
    return () => { cancelled = true; setDayBars((d) => (d?.key === wickKey && d.loading ? null : d)) }
  }, [needDayBars, wickKey])
  const wickCheck = needDayBars && dayBars?.key === wickKey ? dayBars : null
  // 저가 ETF·ETN이 거래량 상위를 차지해 개별 종목이 묻힌다. 항상 제외한다(화면 필터).
  const etpCount = useMemo(() => (data ? data.stocks.filter((s) => s.etp).length : 0), [data])
  const stocks = useMemo(() => {
    if (!data) return []
    const rows = data.stocks.filter((s) => {
      if (s.etp) return false
      if (!closeAtHigh) return true
      if (!(s.high > 0 && s.close >= s.high)) return false
      // 키움 일봉으로 확인한 종목만 시간외 위꼬리로 거른다(확인 못 한 종목은 정규장 기준으로 남긴다).
      const b = wickCheck?.bars?.[s.code]
      return !b || b.close >= b.high
    })
    return sortRows(rows, SORT_KEYS[sort.key], sort.dir)
  }, [data, sort, closeAtHigh, wickCheck])

  const load = useCallback(async (d, rise, amount, change) => {
    setLoading(true)
    setSelectedStock(null)
    setError(null)
    try {
      const res = await api.bigCandle(d, rise, amount, change)
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
  useEffect(() => { load('', minRise, minAmount, minChange) }, [load])

  const submit = (d) => {
    const rise = Math.max(0, Number(minRise) || 0)
    const amount = Math.max(1, Math.round(Number(minAmount) || 150))
    const change = Math.min(30, Math.max(-30, Number(minChange) || 0))
    setMinRise(rise)
    setMinAmount(amount)
    setMinChange(change)
    saveNum(RISE_KEY, rise)
    saveNum(AMOUNT_KEY, amount)
    saveNum(CHANGE_KEY, change)
    load(d, rise, amount, change)
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
        <label htmlFor="bc-change">전일 대비</label>
        <input
          id="bc-change"
          className="num-input"
          type="number"
          min="-30"
          max="30"
          step="any"
          value={minChange}
          onChange={(e) => setMinChange(e.target.value)}
          aria-describedby="bc-change-unit"
          title="전일 종가 대비 등락률. 음수도 가능(예: -5 → 전일 대비 -5% 이상)"
        />
        <span id="bc-change-unit">% 이상</span>
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
        <label className="check-label" title="조회일 종가(장중이면 현재가)가 고가와 같은 종목만 표시">
          <input type="checkbox" checked={closeAtHigh} onChange={(e) => setCloseAtHigh(e.target.checked)} />
          {live ? '현재가' : '종가'} = 고가
        </label>
        {closeAtHigh && data?.source === 'krx' && (
          <label className="check-label"
            title="끄면(기본) 차트처럼 15:30 이후 시간외 체결까지 포함한 키움 일봉으로 위꼬리가 있는 종목을 뺍니다. 켜면 정규장(09:00~15:30) 고가에서 마감한 종목은 시간외 위꼬리가 있어도 보여 줍니다.">
            <input type="checkbox" checked={allowAfterWick} onChange={(e) => setAllowAfterWick(e.target.checked)} />
            시간외 위꼬리 허용
          </label>
        )}
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
            시가 대비 <strong>+{Number(data.min_rise_pct).toFixed(2)}%</strong> 이상,
            전일 대비 <strong>{signedPct(Number(data.min_change_pct ?? 0))}</strong> 이상
            {closeAtHigh && <>, <strong>{priceLabel} = 고가</strong>
              {data.source === 'krx' && (allowAfterWick ? ' (정규장 기준, 시간외 위꼬리 허용)' : ' (시간외 체결 포함)')}</>}
          </span>
          <span className="sum-count">
            {stocks.length}종목
            {etpCount > 0 && <span className="muted"> (ETF·ETN {etpCount}종목 제외)</span>}
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
      {wickCheck && (wickCheck.loading || wickCheck.error || wickCheck.available === false || wickCheck.failed?.length > 0) && (
        <div className="screener-notice" role="status">
          {wickCheck.loading ? '시간외 위꼬리 확인 중(키움 일봉 조회)… 지금은 정규장 기준으로 걸러 보여 줍니다.'
            : wickCheck.error ? `시간외 위꼬리 확인 실패(${wickCheck.error}) — 정규장 기준으로만 걸렀습니다.`
              : wickCheck.available === false ? '실전 키움 계좌가 없어 시간외 위꼬리는 확인하지 못했습니다 — 정규장 기준으로만 걸렀습니다.'
                : `${wickCheck.failed.length}종목은 키움 일봉을 받지 못해 정규장 기준으로만 걸렀습니다(${wickCheck.failed.join(', ')}).`}
        </div>
      )}
      {data && data.etp_known === false && (
        <div className="screener-notice" role="status">
          ETF·ETN 목록을 불러오지 못해 이번 결과에는 ETF·ETN 제외가 적용되지 않았습니다. 잠시 후 다시 조회하세요.
        </div>
      )}

      {error && (
        <div className={error.pending ? 'screener-notice' : 'screener-error'} role={error.pending ? 'status' : 'alert'}>
          <strong>{error.pending ? 'KRX 자료 게시 대기' : '조회 실패'}</strong>
          <p>{error.message}</p>
          <button className="ghost" disabled={loading} onClick={() => submit(date)}>다시 조회</button>
        </div>
      )}

      {loading && !data && <div className="screener-empty">불러오는 중…</div>}

      {data && stocks.length === 0 && !loading && (
        <div className="screener-empty">
          {data.date}에는 조건을 만족하는 종목이 없습니다.
        </div>
      )}

      {data && stocks.length > 0 && (
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
                  title={`${live ? '현재까지 누적 거래량' : '당일 거래량'}`}>
                  거래량{live ? ' (누적)' : ''}
                </SortHeader>
                <SortHeader sortKey="amount" sort={sort} onSort={onSort}
                  title={`${live ? '현재까지 누적 거래대금' : '당일 거래대금'}`}>
                  거래대금{live ? ' (누적)' : ''}
                </SortHeader>
                <th className="col-num">시가</th>
                <th className="col-num">{priceLabel}</th>
                <th className="col-num" title={`${priceLabel} ÷ 시가 − 1`}>시가 대비</th>
                <th className="col-num" title="직전 거래일 종가 대비">전일 대비</th>
              </tr>
            </thead>
            <tbody>
              {stocks.map((s, i) => (
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
                  <td className="col-num">{formatEok(s.market_cap)}</td>
                  <td className="col-num">{won(s.volume)}</td>
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
          // 과거일 목록(KRX 확정 자료)의 고가·종가는 정규장만이고, 차트 일봉(키움)은 15:30 이후
          // 시간외 체결까지 섞는다. '종가 = 고가' 판정 근거를 차트에 정규장 고가 점선으로 보여 준다.
          dayPriceLines={data.source === 'krx' && selectedStock.high > 0
            ? [{ price: selectedStock.high, color: ANCHOR_COLOR, title: '정규장 고가', dashed: true }] : null}
          dayNote={data.source === 'krx' && selectedStock.high > 0
            ? `점선 = ${data.date} 정규장(09:00~15:30) 고가 ${won(selectedStock.high)}, 종가 ${won(selectedStock.close)} (KRX 확정 자료 — 목록·'종가 = 고가' 판정 기준). `
              + '차트 일봉은 키움 자료라 15:30 이후 시간외 체결이 고가·저가·종가에 섞입니다. 점선 위 꼬리는 시간외 거래입니다.'
            : ''}
          includeToday={live}
          defaultInterval={1440}
          anchorDate={data.date}
          register={[{
            label: '대금 양봉 매매 등록',
            title: (account) => `${account.label} 계좌의 대금 양봉 매매 목록에 추가합니다(주문은 나가지 않음). `
              + `${data.date} 봉(시가 ${won(selectedStock.open)} → ${priceLabel} ${won(selectedStock.close)})이 기준 캔들입니다.`,
            run: async (account) => {
              // 기본 설정(매입 구간 여백·2차 기준)으로 2차 매수가 가능한 캔들인지 먼저 알린다.
              const cfg = DEFAULT_CANDLE_CONFIG
              const leg2 = secondLegCheck(zoneOf(selectedStock, cfg.zone_margin_pct), cfg)
              if (leg2 && !leg2.possible && !window.confirm(
                `⚠️ 이 캔들은 2차 매수가 불가능합니다.
`
                + `몸통(시가 대비 +${selectedStock.rise_pct.toFixed(2)}%)이 작아 매입 구간 안 최대 하락률이 `
                + `${leg2.maxDropPct.toFixed(1)}%뿐이라 2차 조건(${addRuleText(cfg)})을 만족할 수 없습니다.
`
                + `기본 설정으로는 1차만 매수됩니다. 그래도 등록할까요?`)) return
              try {
                await api.addCandleTrade(account.id, selectedStock.code, selectedStock.name,
                  { date: data.date, open: selectedStock.open, close: selectedStock.close })
                navigate(ROUTES.CANDLE_TRADE)
              } catch (e) {
                window.alert(`대금 양봉 매매 등록 실패: ${e.message || e}`)
              }
            },
          }, {
            label: '구간 매매 등록',
            title: (account) => `${account.label} 계좌의 [구간 매매] 목록에 추가합니다(주문은 나가지 않음). 매입 구간은 거기서 차트로 지정합니다.`,
            run: async (account) => {
              try {
                await api.addZoneTrade(account.id, selectedStock.code, selectedStock.name)
                navigate(ROUTES.ZONE_TRADE)
              } catch (e) {
                window.alert(`구간 매매 등록 실패: ${e.message || e}`)
              }
            },
          }]}
          onClose={() => setSelectedStock(null)}
        />
      )}
    </main>
  )
}
