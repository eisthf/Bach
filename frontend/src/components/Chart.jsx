// lightweight-charts 캔들 차트.
// - 캔들: 내부 투명, 테두리만. 상승(close>=open) 적색 / 하락 청색.
// - SMA 5/10/20/60 겹쳐 그림 (파랑/분홍/주황/초록).
// - 크로스헤어: 내장. 마우스 가격 수평선 + 가격 라벨(손절선 가늠용).
// - 실시간 틱으로 마지막 봉 갱신. 봉 경계를 넘으면 서버에서 재조회(아래 참고).
import React, { useCallback, useEffect, useRef } from 'react'
import { createChart, CrosshairMode } from 'lightweight-charts'
import { api } from '../api'
import { sma, lastSma, MA_LINES } from '../indicators'

// 봉 경계를 넘었는데 서버 봉이 아직 안 만들어졌을 때, 매 틱 재조회하지 않도록.
const REFRESH_COOLDOWN_MS = 10_000

export default function Chart({ account, code, interval, tick, height = 360 }) {
  const containerRef = useRef(null)
  const chartRef = useRef(null)
  const candleRef = useRef(null)
  const maRefs = useRef([])
  const barsRef = useRef([])
  const dayStartRef = useRef(0)
  const refreshingRef = useRef(false)
  const lastRefreshRef = useRef(0)

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
      rightPriceScale: { borderColor: '#ddd' },
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
    const maSeries = MA_LINES.map((m) =>
      chart.addLineSeries({ color: m.color, lineWidth: 1, priceLineVisible: false, title: m.title }),
    )

    chartRef.current = chart
    candleRef.current = candle
    maRefs.current = maSeries

    return () => {
      chart.remove()
      chartRef.current = null
      candleRef.current = null
      maRefs.current = []
    }
  }, [])

  // 봉 데이터 로드 (interval 변경 시)
  useEffect(() => {
    let cancelled = false
    chartRef.current?.timeScale().applyOptions({
      timeVisible: interval !== 1440,
      secondsVisible: false,
    })
    api.getBars(account, code, interval).then((data) => {
      if (cancelled || !candleRef.current || !chartRef.current) return
      const bars = data.bars
      barsRef.current = bars
      dayStartRef.current = data.day_start_index
      candleRef.current.setData(bars)
      MA_LINES.forEach((m, i) => {
        maRefs.current[i].setData(sma(bars, m.period))
      })
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
        // 당일 구간으로 시야 이동(이전 60봉은 SMA 계산용이라 살짝만 보이게)
        if (bars.length > data.day_start_index) {
          const fromIndex = interval === 1440
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
    })
    return () => {
      cancelled = true
    }
  }, [account, code, interval])

  // 봉 경계를 넘었을 때 서버에서 다시 받는다.
  // 클라이언트가 틱으로 새 봉을 지어내지 않는 이유: 틱은 큐가 차면 드롭될 수
  // 있고 거래량도 실려오지 않아, 만들어낸 봉의 고가/저가/거래량이 실제와
  // 달라진다. 봉의 진실원은 서버(ka10080)다.
  const refreshBars = useCallback(async () => {
    if (refreshingRef.current) return
    refreshingRef.current = true
    try {
      const data = await api.getBars(account, code, interval)
      if (!candleRef.current) return
      barsRef.current = data.bars
      dayStartRef.current = data.day_start_index
      candleRef.current.setData(data.bars)
      MA_LINES.forEach((m, i) => maRefs.current[i].setData(sma(data.bars, m.period)))
      // 시야(setVisibleRange)는 건드리지 않는다 — 사용자의 확대/이동 유지.
    } catch {
      // 실패해도 다음 경계에서 재시도. 그 사이 마지막 봉은 틱으로 계속 갱신됨.
    } finally {
      refreshingRef.current = false
    }
  }, [account, code, interval])

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
    }
    bars[bars.length - 1] = { ...last, ...updated }
    candleRef.current.update(updated)
    // 마지막 봉 변동으로 SMA 끝점도 갱신(끝점만 필요 → O(period))
    MA_LINES.forEach((m, i) => {
      const point = lastSma(bars, m.period)
      if (point) maRefs.current[i].update(point)
    })
  }, [tick, interval, refreshBars])

  return (
    <div
      ref={containerRef}
      className="chart-canvas"
      style={{ width: '100%', '--chart-height': `${height}px` }}
    />
  )
}
