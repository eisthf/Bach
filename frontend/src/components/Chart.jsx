// lightweight-charts 캔들 차트.
// - 캔들: 내부 투명, 테두리만. 상승(close>=open) 적색 / 하락 청색.
// - SMA 5/10/20/60 겹쳐 그림 (파랑/분홍/주황/초록).
// - 엔벨로프(중심 SMA20, ±10%) 상·하단선: [엔벨로프] 단추로 켜고 끔(차트별로 브라우저에 기억).
// - 거래량: 하단 막대. 상승봉 적색 / 하락봉 청색.
// - 크로스헤어: 내장. 마우스 가격 수평선 + 가격 라벨(손절선 가늠용).
// - 실시간 틱으로 마지막 봉 갱신. 봉 경계를 넘으면 서버에서 재조회(아래 참고).
import React, { useCallback, useEffect, useRef, useState } from 'react'
import { createChart, CrosshairMode } from 'lightweight-charts'
import { api } from '../api'
import { sma, lastSma, MA_LINES, ENVELOPE, envelope } from '../indicators'
import { ANCHOR_COLOR, anchorMarker, tradeMarkers, tradePoints } from '../tradeMarkers'
import { TradeCrossPrimitive } from '../tradeCrossPrimitive'
import { AmountStripePrimitive, amountStripeTimes } from '../amountStripePrimitive'
import { useChartEnvelope } from '../chartPrefs'

// 봉 경계를 넘었는데 서버 봉이 아직 안 만들어졌을 때, 매 틱 재조회하지 않도록.
const REFRESH_COOLDOWN_MS = 10_000

// 차트 높이 배율(기본 / 1.5배). 마지막 선택을 브라우저에 기억해 새로 여는 차트에도 쓴다.
const TALL_SCALE = 1.5
const TALL_KEY = 'bach.chartTall'
const readTall = () => { try { return localStorage.getItem(TALL_KEY) === '1' } catch { return false } }
const writeTall = (v) => { try { localStorage.setItem(TALL_KEY, v ? '1' : '0') } catch { /* 저장 불가면 이번 화면만 */ } }

const volumePoint = (bar) => ({
  time: bar.time,
  value: Number(bar.volume || 0),
  color: bar.close >= bar.open ? 'rgba(211, 47, 47, 0.35)' : 'rgba(21, 101, 192, 0.35)',
})

// anchorDate: 일봉일 때 이 날짜(YYYY-MM-DD) 앞 20거래일부터 60거래일을 보여준다(스크리너 조회일).
// priceLines: [{price, color, title, dashed}] 수평 가격선(예: 대금 양봉 매매 구간·익절선).
// onPriceClick: 지정하면 차트를 누른 위치의 가격으로 호출된다(구간 매매의 수평선 지정). 커서가 십자로 바뀐다.
// draggableLines: [{id, price}] 마우스·터치로 위아래로 끌 수 있는 선. 같은 선을 priceLines에
//   dragId: id 로 넘기면, 끄는 동안엔 차트 안에서만 그 선을 옮긴다(부모 재렌더·레이아웃 이동 없음).
// onLineDrag(id, price): 놓을 때 한 번, 마지막 가격으로 호출.
// [측정] 단추(모든 차트): 켜고 두 점을 누르면 두 점을 잇는 선과 변화율·봉 수를 보여준다
//   (화면 표시만, 저장 안 함). 세 번째 누르면 새로 잰다. 봉 단위를 바꾸면 지운다.
//   측정 중엔 클릭이 측정으로 가고 onPriceClick(가격 찍기)은 쉰다.
// prefKey: 차트 자리 이름(예: 'manual:mock:005930'). 엔벨로프 켜고 끔을 이 이름별로 기억한다.
//   없으면 '계좌:종목'.
export default function Chart({ prefKey = null, account, code, interval, tick, height = 360, sessionOnly = false, includeToday = false, extended = false, anchorDate = null, priceLines = null, onPriceClick = null, draggableLines = null, onLineDrag = null, onSessionDate }) {
  const anchor = interval === 1440 ? anchorDate : null
  const anchorRef = useRef(anchor)
  anchorRef.current = anchor
  const containerRef = useRef(null)
  const chartRef = useRef(null)
  const candleRef = useRef(null)
  const volumeRef = useRef(null)
  const maRefs = useRef([])
  const envRefs = useRef([])   // [상단, 하단]
  const barsRef = useRef([])
  const dayStartRef = useRef(0)
  const refreshingRef = useRef(false)
  const lastRefreshRef = useRef(0)
  const [loadState, setLoadState] = useState('loading')
  const [loadError, setLoadError] = useState('')
  const [retry, setRetry] = useState(0)
  const tradesRef = useRef([])
  const crossRef = useRef(null)
  const stripeRef = useRef(null)
  const [tradeError, setTradeError] = useState(false)

  // 봉 위/아래 화살표·라벨(봉별 합산) + 캔들 안 체결 건별 X. 조회일이 있으면 기준봉 화살표도.
  const drawTrades = (bars) => {
    const markers = tradeMarkers(bars, tradesRef.current, interval)
    const anchorMark = anchorMarker(bars, anchorRef.current)
    if (anchorMark) markers.push(anchorMark)
    candleRef.current?.setMarkers(markers.sort((a, b) => a.time - b.time))
    crossRef.current?.setPoints(tradePoints(bars, tradesRef.current, interval))
  }
  // 거래대금은 일봉에만 있다(분봉 원천엔 없음).
  const drawStripes = (bars) => {
    stripeRef.current?.setTimes(interval === 1440 ? amountStripeTimes(bars) : [])
  }

  useEffect(() => {
    let cancelled = false
    tradesRef.current = []
    const load = async () => {
      try {
        const data = await api.trades(account, code)
        if (cancelled) return
        tradesRef.current = data.trades || []
        drawTrades(barsRef.current)
        setTradeError(false)
      } catch { if (!cancelled) setTradeError(true) }
    }
    load()
    const timer = setInterval(load, 5000)
    return () => { cancelled = true; clearInterval(timer) }
  }, [account, code, interval])
  const [displayVolume, setDisplayVolume] = useState(null)
  const [tall, setTall] = useState(readTall)
  const chooseTall = (v) => { setTall(v); writeTall(v) }
  const [envOn, setEnvOn] = useChartEnvelope(prefKey || `${account}:${code}`)
  const envOnRef = useRef(envOn)
  envOnRef.current = envOn
  const toggleEnv = () => {
    const v = !envOn
    setEnvOn(v)
    envRefs.current.forEach((series) => series.applyOptions({ visible: v }))
  }
  // 엔벨로프 데이터는 켜고 끔과 무관하게 늘 채워 둔다(숨긴 시리즈는 그리지도, 축 범위에 넣지도 않는다).
  const drawEnvelope = (bars) => {
    const { upper, lower } = envelope(bars)
    envRefs.current[0]?.setData(upper)
    envRefs.current[1]?.setData(lower)
  }

  // 차트 생성 (1회)
  useEffect(() => {
    const el = containerRef.current
    const chart = createChart(el, {
      // autoSize: 내장 ResizeObserver로 컨테이너 크기에 맞춰 자동 사이징.
      // 카드가 추가된 직후(컨테이너 측정 전) 생성돼도 레이아웃이 잡히면
      // 자동으로 리페인트되어 '캔들이 안 보이는' 문제를 막는다.
      autoSize: true,
      layout: { background: { color: '#ffffff' }, textColor: '#333' },
      grid: {
        vertLines: { color: '#f0f0f0' },
        horzLines: { color: '#f0f0f0' },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        horzLine: { labelVisible: true, color: '#888', width: 1, style: 2 },
        vertLine: { labelVisible: true, color: '#888', width: 1, style: 2 },
      },
      rightPriceScale: {
        borderColor: '#ddd',
        scaleMargins: { top: 0.05, bottom: 0.25 },
      },
      leftPriceScale: { visible: true, borderColor: '#ddd' },
      timeScale: { borderColor: '#ddd', timeVisible: true, secondsVisible: false },
    })
    // 테두리만 있는 캔들: body 투명, border 색만.
    const candle = chart.addCandlestickSeries({
      upColor: 'rgba(0,0,0,0)',
      downColor: 'rgba(0,0,0,0)',
      wickUpColor: '#d32f2f',
      wickDownColor: '#1565c0',
      borderUpColor: '#d32f2f',   // 상승: 적색
      borderDownColor: '#1565c0', // 하락: 청색
      borderVisible: true,
    })
    const cross = new TradeCrossPrimitive()
    candle.attachPrimitive(cross)
    const stripe = new AmountStripePrimitive()
    candle.attachPrimitive(stripe)
    const maSeries = MA_LINES.map((m) =>
      chart.addLineSeries({ color: m.color, lineWidth: 1, priceLineVisible: false, title: m.title }),
    )
    const envSeries = [`+${ENVELOPE.pct}%`, `-${ENVELOPE.pct}%`].map((label) =>
      chart.addLineSeries({
        color: ENVELOPE.color, lineWidth: 1, priceLineVisible: false, crosshairMarkerVisible: false,
        title: `Env${ENVELOPE.period} ${label}`, visible: envOnRef.current,
      }),
    )
    const volume = chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: 'left',
      priceLineVisible: false,
      lastValueVisible: false,
    })
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } })
    const showVolumeAtCrosshair = (param) => {
      const selected = param.seriesData.get(volume)
      const latest = barsRef.current[barsRef.current.length - 1]
      setDisplayVolume(Number(selected?.value ?? latest?.volume ?? 0))
    }
    chart.subscribeCrosshairMove(showVolumeAtCrosshair)

    chartRef.current = chart
    candleRef.current = candle
    crossRef.current = cross
    stripeRef.current = stripe
    volumeRef.current = volume
    maRefs.current = maSeries
    envRefs.current = envSeries

    return () => {
      chart.unsubscribeCrosshairMove(showVolumeAtCrosshair)
      chart.remove()
      chartRef.current = null
      candleRef.current = null
      crossRef.current = null
      stripeRef.current = null
      volumeRef.current = null
      maRefs.current = []
      envRefs.current = []
    }
  }, [])

  // 두 점 측정. 점은 (봉 시각, 가격). 첫 점을 찍으면 포인터까지 점선으로 이어 실시간 변화율을
  // 보여 주고(미리보기), 두 번째 점을 찍으면 실선으로 확정한다. 선은 라인 시리즈 하나로 그린다.
  const [measureOn, setMeasureOn] = useState(false)
  const measureOnRef = useRef(false)
  measureOnRef.current = measureOn
  const [measurePts, setMeasurePts] = useState([])   // 찍은 점(최대 2)
  const measurePtsRef = useRef([])
  measurePtsRef.current = measurePts
  const [hoverPt, setHoverPt] = useState(null)       // 첫 점 뒤 포인터 위치 {time, price, x, y}
  const measureSeriesRef = useRef(null)
  const clearMeasure = () => {
    setMeasurePts([])
    setHoverPt(null)
    if (measureSeriesRef.current) {
      chartRef.current?.removeSeries(measureSeriesRef.current)
      measureSeriesRef.current = null
    }
  }
  useEffect(() => { clearMeasure() }, [interval, code]) // eslint-disable-line react-hooks/exhaustive-deps
  // 포인터 위치의 (봉 시각, 가격). 마지막 봉 오른쪽 빈 곳이면 가장 가까운 봉으로.
  const pointAt = (param) => {
    if (!param.point || !candleRef.current) return null
    const price = candleRef.current.coordinateToPrice(param.point.y)
    let time = param.time
    if (time == null && param.logical != null) {
      const bars = barsRef.current
      if (bars.length) time = bars[Math.max(0, Math.min(bars.length - 1, Math.round(param.logical)))].time
    }
    return price > 0 && time != null ? { time, price, x: param.point.x, y: param.point.y } : null
  }
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return undefined
    const onMove = (param) => {
      if (!measureOnRef.current || measurePtsRef.current.length !== 1) return
      // 미리보기 선을 다시 그리면(setData) 같은 위치로 crosshairMove가 또 온다 — 같으면 무시해야
      // 렌더 → 그리기 → 이벤트가 무한히 돌지 않는다.
      const pt = pointAt(param)
      setHoverPt((prev) => (prev && pt && prev.x === pt.x && prev.y === pt.y && prev.time === pt.time) || prev === pt ? prev : pt)
    }
    chart.subscribeCrosshairMove(onMove)
    return () => chart.unsubscribeCrosshairMove(onMove)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  const measurePair = measurePts.length === 2 ? measurePts
    : measurePts.length === 1 && hoverPt ? [measurePts[0], hoverPt] : null
  const previewing = measurePts.length === 1
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const pts = measurePair || measurePts
    if (!pts.length) {
      if (measureSeriesRef.current) {
        chart.removeSeries(measureSeriesRef.current)
        measureSeriesRef.current = null
      }
      return
    }
    if (!measureSeriesRef.current) {
      measureSeriesRef.current = chart.addLineSeries({
        color: '#7b1fa2', lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false, pointMarkersVisible: true, pointMarkersRadius: 4,
        autoscaleInfoProvider: () => null,
      })
    }
    measureSeriesRef.current.applyOptions({ lineStyle: previewing ? 2 : 0 })
    const sorted = [...pts].sort((x, y) => x.time - y.time)
    // 같은 봉이면 점 하나(첫 점)만.
    const data = sorted.length === 2 && sorted[0].time !== sorted[1].time
      ? sorted.map((p) => ({ time: p.time, value: p.price }))
      : [{ time: measurePts[0].time, value: measurePts[0].price }]
    measureSeriesRef.current.setData(data)
  }, [measurePts, hoverPt]) // eslint-disable-line react-hooks/exhaustive-deps

  // 가격 찍기. 콜백은 ref로 들고 구독은 한 번만 한다(부모가 매 렌더 새 함수를 넘겨도).
  const priceClickRef = useRef(onPriceClick)
  priceClickRef.current = onPriceClick
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return undefined
    const onClick = (param) => {
      if (measureOnRef.current) {
        if (Date.now() < suppressClickUntilRef.current) return
        const pt = pointAt(param)
        if (!pt) return
        setHoverPt(null)
        setMeasurePts((pts) => (pts.length >= 2 ? [] : pts).concat({ time: pt.time, price: pt.price }))
        return
      }
      const cb = priceClickRef.current
      // 선을 끌고 놓은 직후의 클릭은 '찍기'로 치지 않는다.
      if (!cb || !param.point || !candleRef.current || Date.now() < suppressClickUntilRef.current) return
      const price = candleRef.current.coordinateToPrice(param.point.y)
      if (price !== null && price > 0) cb(price)
    }
    chart.subscribeClick(onClick)
    return () => chart.unsubscribeClick(onClick)
  }, [])

  // 선 끌기. 선 근처(마우스 6px, 터치 16px)에서 누르면 그 선을 잡는다. 잡은 동안엔 차트의
  // 스크롤·확대와 페이지 스크롤(터치)을 막는다. 캡처 단계에서 가로채 차트 캔버스까지 가지 않게 한다.
  const dragLinesRef = useRef(draggableLines)
  dragLinesRef.current = draggableLines
  const lineDragRef = useRef(onLineDrag)
  lineDragRef.current = onLineDrag
  const suppressClickUntilRef = useRef(0)
  useEffect(() => {
    const el = containerRef.current
    if (!el) return undefined
    let dragging = null
    const yOf = (e) => {
      const t = e.touches ? (e.touches[0] || e.changedTouches[0]) : e
      return t.clientY - el.getBoundingClientRect().top
    }
    const hit = (y, touch) => {
      const candle = candleRef.current
      if (!candle || !lineDragRef.current) return null
      const tol = touch ? 16 : 6
      let best = null
      let bestD = Infinity
      for (const line of dragLinesRef.current || []) {
        if (!(line.price > 0)) continue
        const c = candle.priceToCoordinate(line.price)
        if (c === null) continue
        const d = Math.abs(c - y)
        if (d <= tol && d < bestD) { best = line.id; bestD = d }
      }
      return best
    }
    let lastPrice = null
    const move = (e) => {
      if (!dragging) return
      if (e.cancelable) e.preventDefault()
      const price = candleRef.current?.coordinateToPrice(yOf(e))
      if (price === null || !(price > 0)) return
      lastPrice = price
      dragLineObjsRef.current[dragging]?.applyOptions({ price })
    }
    const end = () => {
      if (!dragging) return
      const id = dragging
      const price = lastPrice
      dragging = null
      lastPrice = null
      chartRef.current?.applyOptions({ handleScroll: true, handleScale: true })
      el.classList.remove('dragging-line')
      suppressClickUntilRef.current = Date.now() + 400
      window.removeEventListener('mousemove', move)
      window.removeEventListener('touchmove', move)
      window.removeEventListener('mouseup', end)
      window.removeEventListener('touchend', end)
      window.removeEventListener('touchcancel', end)
      if (price !== null) lineDragRef.current?.(id, price)
    }
    const start = (e) => {
      const touch = e.type === 'touchstart'
      if (touch && e.touches.length !== 1) return
      if (!touch && e.button !== 0) return
      const id = hit(yOf(e), touch)
      if (!id) return
      e.stopPropagation()
      if (e.cancelable) e.preventDefault()
      dragging = id
      chartRef.current?.applyOptions({ handleScroll: false, handleScale: false })
      el.classList.add('dragging-line')
      if (touch) {
        window.addEventListener('touchmove', move, { passive: false })
        window.addEventListener('touchend', end)
        window.addEventListener('touchcancel', end)
      } else {
        window.addEventListener('mousemove', move)
        window.addEventListener('mouseup', end)
      }
    }
    const hover = (e) => {
      if (!dragging) el.classList.toggle('line-hover', !!hit(yOf(e), false))
    }
    el.addEventListener('mousedown', start, true)
    el.addEventListener('touchstart', start, { capture: true, passive: false })
    el.addEventListener('mousemove', hover)
    return () => {
      end()
      el.removeEventListener('mousedown', start, true)
      el.removeEventListener('touchstart', start, { capture: true })
      el.removeEventListener('mousemove', hover)
    }
  }, [])

  // 수평 가격선. 내용이 바뀔 때만 다시 그린다(부모가 매 렌더 새 배열을 넘겨도).
  // dragId 가 있는 선은 끌기 중에 직접 옮길 수 있게 객체를 들고 있는다.
  const dragLineObjsRef = useRef({})
  const linesKey = JSON.stringify(priceLines || [])
  useEffect(() => {
    const candle = candleRef.current
    if (!candle) return undefined
    const objs = {}
    const created = (priceLines || []).filter((l) => l && l.price > 0).map((l) => {
      const line = candle.createPriceLine({
        price: l.price, color: l.color || '#888', lineWidth: 1, lineStyle: l.dashed ? 2 : 0,
        axisLabelVisible: true, title: l.title || '',
      })
      if (l.dragId) objs[l.dragId] = line
      return line
    })
    dragLineObjsRef.current = objs
    return () => created.forEach((line) => candleRef.current?.removePriceLine(line))
  }, [linesKey])

  // 봉 데이터 로드 (interval 변경 시)
  useEffect(() => {
    let cancelled = false
    setLoadState('loading')
    barsRef.current = []
    stripeRef.current?.setTimes([])
    setDisplayVolume(null)
    candleRef.current?.setData([])
    volumeRef.current?.setData([])
    maRefs.current.forEach((series) => series.setData([]))
    envRefs.current.forEach((series) => series.setData([]))
    chartRef.current?.timeScale().applyOptions({
      timeVisible: interval !== 1440,
      secondsVisible: false,
    })
    api.getBars(account, code, interval, undefined, sessionOnly && interval !== 1440, includeToday, extended, anchor).then((data) => {
      if (cancelled || !candleRef.current || !chartRef.current) return
      const bars = data.bars
      onSessionDate?.(data.session_date)
      setLoadState(bars.length ? 'ready' : 'empty')
      barsRef.current = bars
      setDisplayVolume(bars.length ? Number(bars[bars.length - 1].volume || 0) : null)
      dayStartRef.current = data.day_start_index
      candleRef.current.setData(bars)
      drawTrades(bars)
      drawStripes(bars)
      volumeRef.current?.setData(bars.map(volumePoint))
      MA_LINES.forEach((m, i) => {
        maRefs.current[i].setData(sma(bars, m.period))
      })
      drawEnvelope(bars)
      // 시야 이동은 컨테이너가 실제 너비를 가진 뒤에 적용한다.
      // autoSize의 ResizeObserver는 비동기라, 카드가 막 추가돼 너비가 0인
      // 상태에서 setVisibleRange를 호출하면 빈 범위가 '고정'되어 캔들이
      // 안 보인다(간헐적 실패의 원인). 너비가 생길 때까지 프레임마다 재시도.
      let tries = 0
      const applyView = () => {
        if (cancelled || !chartRef.current || !containerRef.current) return
        if (containerRef.current.clientWidth === 0 && tries++ < 120) {
          requestAnimationFrame(applyView)
          return
        }
        const ts = chartRef.current.timeScale()
        // 스크리너는 정규장 하루의 첫 봉부터, 매매 차트는 이전 5봉을 곁들여 보여준다.
        if (bars.length > data.day_start_index) {
          const fromIndex = interval === 1440 || sessionOnly
            ? data.day_start_index
            : Math.max(0, data.day_start_index - 5)
          ts.setVisibleRange({
            from: bars[fromIndex].time,
            to: bars[bars.length - 1].time,
          })
        } else {
          ts.fitContent()
        }
      }
      requestAnimationFrame(applyView)
    }).catch((error) => {
      if (cancelled) return
      setLoadError(error.message || '차트를 불러오지 못했습니다.')
      setLoadState('error')
    })
    return () => {
      cancelled = true
    }
  }, [account, code, interval, sessionOnly, includeToday, extended, anchor, retry])

  // 봉 경계를 넘었을 때 서버에서 다시 받는다.
  // 클라이언트가 틱으로 새 봉을 지어내지 않는 이유: 틱은 큐가 차면 드롭될 수
  // 있고 거래량도 실려오지 않아, 만들어낸 봉의 고가/저가/거래량이 실제와
  // 달라진다. 봉의 진실원은 서버(ka10080)다.
  const refreshBars = useCallback(async () => {
    if (refreshingRef.current) return
    refreshingRef.current = true
    try {
      const data = await api.getBars(account, code, interval, undefined, sessionOnly && interval !== 1440, includeToday, extended, anchor)
      if (!candleRef.current) return
      barsRef.current = data.bars
      setDisplayVolume(data.bars.length ? Number(data.bars[data.bars.length - 1].volume || 0) : null)
      dayStartRef.current = data.day_start_index
      candleRef.current.setData(data.bars)
      drawTrades(data.bars)
      drawStripes(data.bars)
      volumeRef.current?.setData(data.bars.map(volumePoint))
      MA_LINES.forEach((m, i) => maRefs.current[i].setData(sma(data.bars, m.period)))
      drawEnvelope(data.bars)
      // 시야(setVisibleRange)는 건드리지 않는다 — 사용자의 확대/이동 유지.
    } catch {
      // 실패해도 다음 경계에서 재시도. 그 사이 마지막 봉은 틱으로 계속 갱신됨.
    } finally {
      refreshingRef.current = false
    }
  }, [account, code, interval, sessionOnly, includeToday, extended, anchor])

  // 실시간 틱 → 마지막 봉 갱신 (경계를 넘으면 재조회)
  useEffect(() => {
    if (!tick || !candleRef.current) return
    const bars = barsRef.current
    if (!bars.length) return
    const last = bars[bars.length - 1]

    // 틱과 봉이 같은 시간축(KST 벽시계를 UTC로 간주한 epoch)이라 봉 경계를
    // 직접 계산할 수 있다. 예전엔 틱만 진짜 epoch이라 9시간 어긋나 이 판정이
    // 불가능했고, 그래서 마지막 봉 하나가 무한히 커졌다.
    if (tick.time >= last.time + interval * 60) {
      const now = Date.now()
      if (now - lastRefreshRef.current > REFRESH_COOLDOWN_MS) {
        lastRefreshRef.current = now
        refreshBars()
      }
      return // 새 구간의 가격으로 '이전' 봉을 오염시키지 않는다
    }

    const updated = {
      time: last.time,
      open: last.open,
      // 일봉은 서버 캐시를 하루 동안 유지하므로 실시간 틱의 당일 고저를
      // 사용해 새 브라우저에서도 오늘 꼬리를 즉시 정확하게 맞춘다.
      high: Math.max(last.high, tick.price, interval === 1440 ? tick.high : tick.price),
      low: Math.min(last.low, tick.price, interval === 1440 ? tick.low : tick.price),
      close: tick.price,
      volume: Number(last.volume || 0) + Number(tick.volume || 0),
    }
    // 일봉 거래대금: 틱의 당일 누적값(0이면 원천 미제공 → 기존 값 유지).
    const amount = interval === 1440 ? Math.max(Number(last.amount || 0), Number(tick.amount || 0)) : last.amount
    bars[bars.length - 1] = { ...last, ...updated, amount }
    if (amount !== last.amount) drawStripes(bars)
    candleRef.current.update(updated)
    volumeRef.current?.update(volumePoint(updated))
    setDisplayVolume(updated.volume)
    // 마지막 봉 변동으로 SMA 끝점도 갱신(끝점만 필요 → O(period))
    MA_LINES.forEach((m, i) => {
      const point = lastSma(bars, m.period)
      if (point) maRefs.current[i].update(point)
    })
    const mid = lastSma(bars, ENVELOPE.period)
    if (mid) {
      const k = ENVELOPE.pct / 100
      envRefs.current[0]?.update({ time: mid.time, value: mid.value * (1 + k) })
      envRefs.current[1]?.update({ time: mid.time, value: mid.value * (1 - k) })
    }
  }, [tick, interval, refreshBars])

  const timeLabel = (t) => {
    const d = new Date(t * 1000)  // KST 벽시계를 UTC로 둔 epoch → UTC 필드가 곧 KST
    const md = `${d.getUTCMonth() + 1}/${d.getUTCDate()}`
    return interval === 1440 ? md : `${md} ${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`
  }
  const priceLabel = (p) => Math.round(p).toLocaleString('ko-KR')
  // 두 점 사이 변화율·봉 수(왼쪽 점 → 오른쪽 점).
  const measureOf = (pair) => {
    const [a, b] = [...pair].sort((x, y) => x.time - y.time)
    const pct = (b.price - a.price) / a.price * 100
    const idx = (t) => barsRef.current.findIndex((bar) => bar.time === t)
    const ia = idx(a.time)
    const n = ia >= 0 ? idx(b.time) - ia : null
    return { a, b, pctText: `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`, bars: n != null && n >= 0 ? `${n}봉` : '', up: pct >= 0 }
  }
  const measured = measurePair ? measureOf(measurePair) : null
  let measureText = '첫 점을 차트에서 누르세요'
  if (measured) {
    measureText = `${priceLabel(measured.a.price)} (${timeLabel(measured.a.time)}) → ${priceLabel(measured.b.price)} `
      + `(${timeLabel(measured.b.time)}) = ${measured.pctText}${measured.bars ? ` · ${measured.bars}` : ''}`
      + (previewing ? ' — 두 번째 점을 누르면 확정' : '')
  } else if (measurePts.length === 1) {
    measureText = `첫 점 ${priceLabel(measurePts[0].price)} (${timeLabel(measurePts[0].time)}) — 두 번째 점을 누르세요`
  }
  // 미리보기 중엔 포인터 옆에 변화율을 띄운다.
  const floatLabel = previewing && hoverPt && measured
    ? { x: hoverPt.x, y: hoverPt.y, text: `${measured.pctText}${measured.bars ? ` · ${measured.bars}` : ''}`, up: measured.up }
    : null

  return (
    <div className="chart-container">
      <div className="chart-measure">
        <button type="button" className={`measure-btn env-btn${envOn ? ' on' : ''}`} aria-pressed={envOn}
          onClick={toggleEnv}
          title={`엔벨로프: 중심 ${ENVELOPE.period}봉 이동평균, 폭 ±${ENVELOPE.pct}%`}>
          엔벨로프{envOn ? ' 끄기' : ''}
        </button>
        <button type="button" className={`measure-btn${measureOn ? ' on' : ''}`} aria-pressed={measureOn}
          onClick={() => { setMeasureOn((v) => !v); clearMeasure() }}
          title="두 점을 눌러 변화율을 잽니다">
          📏 측정{measureOn ? ' 끄기' : ''}
        </button>
        {measureOn && (
          <span className={`measure-result${measurePts.length === 2 ? ' done' : ''}`} aria-live="polite">
            {measureText}
            {measurePts.length > 0 && <button type="button" className="link-btn" onClick={clearMeasure}>지우기</button>}
          </span>
        )}
      </div>
      <div className="chart-trade-legend"><span className="up">▲ B 매수 체결</span> · <span className="down">▼ S 매도 체결</span> · 캔들 안 ✕ = 체결 건별 체결가 · 수량·평균 체결가 · 시각 미제공 건은 수신 시각 기준{interval === 1440 && <> · <span className="amount-stripe-legend">노란 띠</span> = 거래대금 150억원 이상</>}{anchor && <> · <span style={{ color: ANCHOR_COLOR }}>▼ 기준봉</span> = 조회일 {anchor}</>}</div>
      {tradeError && <div role="status">체결 표시를 갱신하지 못했습니다. 재연결 중…</div>}
      {loadState === 'loading' && <p role="status">차트 불러오는 중…</p>}
      {loadState === 'empty' && <p role="status">조회 가능한 봉 데이터가 없습니다.</p>}
      {loadState === 'error' && <div role="alert">{loadError} <button onClick={() => setRetry((value) => value + 1)}>다시 시도</button></div>}
      <div className="chart-volume-value" aria-live="off">
        {loadState === 'ready' && displayVolume !== null && (
          <>
            거래량 <strong>{displayVolume.toLocaleString('ko-KR')}주</strong>
            <span>봉에 마우스를 올리면 해당 봉의 수량을 표시합니다.</span>
          </>
        )}
        <div className="chart-height-toggle" role="group" aria-label="차트 높이">
          <button type="button" className={tall ? '' : 'on'} aria-pressed={!tall} onClick={() => chooseTall(false)}>기본</button>
          <button type="button" className={tall ? 'on' : ''} aria-pressed={tall} onClick={() => chooseTall(true)}>1.5배</button>
        </div>
      </div>
    <div className="chart-canvas-wrap">
      <div
        ref={containerRef}
        className={`chart-canvas${onPriceClick || measureOn ? ' picking' : ''}`}
        style={{
          width: '100%',
          '--chart-height': `${Math.round(height * (tall ? TALL_SCALE : 1))}px`,
          '--chart-max-mobile': `${tall ? Math.round(280 * TALL_SCALE) : 280}px`,
        }}
      />
      {floatLabel && (
        <div className={`measure-float ${floatLabel.up ? 'up' : 'down'}`}
          style={(containerRef.current && floatLabel.x > containerRef.current.clientWidth * 0.7)
            ? { left: floatLabel.x - 14, top: Math.max(0, floatLabel.y - 30), transform: 'translateX(-100%)' }
            : { left: floatLabel.x + 14, top: Math.max(0, floatLabel.y - 30) }}>
          {floatLabel.text}
        </div>
      )}
    </div>
    </div>
  )
}
