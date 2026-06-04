// lightweight-charts 캔들 차트.
// - 캔들: 내부 투명, 테두리만. 상승(close>=open) 적색 / 하락 청색.
// - SMA 5/10/20/60 겹쳐 그림 (파랑/분홍/주황/초록).
// - 크로스헤어: 내장. 마우스 가격 수평선 + 가격 라벨(손절선 가늠용).
// - 실시간 틱으로 마지막 봉 갱신.
import React, { useEffect, useRef } from 'react'
import { createChart, CrosshairMode } from 'lightweight-charts'
import { api } from '../api'
import { sma, MA_LINES } from '../indicators'

export default function Chart({ code, interval, tick }) {
  const containerRef = useRef(null)
  const chartRef = useRef(null)
  const candleRef = useRef(null)
  const maRefs = useRef([])
  const barsRef = useRef([])
  const dayStartRef = useRef(0)

  // 차트 생성 (1회)
  useEffect(() => {
    const el = containerRef.current
    const chart = createChart(el, {
      width: el.clientWidth,
      height: 360,
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

    const onResize = () => chart.applyOptions({ width: el.clientWidth })
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      chart.remove()
    }
  }, [])

  // 봉 데이터 로드 (interval 변경 시)
  useEffect(() => {
    let cancelled = false
    api.getBars(code, interval).then((data) => {
      if (cancelled) return
      const bars = data.bars
      barsRef.current = bars
      dayStartRef.current = data.day_start_index
      candleRef.current.setData(bars)
      MA_LINES.forEach((m, i) => {
        maRefs.current[i].setData(sma(bars, m.period))
      })
      // 당일 구간으로 시야 이동(이전 60봉은 SMA 계산용이라 살짝만 보이게)
      const ts = chartRef.current.timeScale()
      if (bars.length > data.day_start_index) {
        ts.setVisibleRange({
          from: bars[Math.max(0, data.day_start_index - 5)].time,
          to: bars[bars.length - 1].time,
        })
      } else {
        ts.fitContent()
      }
    })
    return () => {
      cancelled = true
    }
  }, [code, interval])

  // 실시간 틱 → 마지막 봉 갱신
  useEffect(() => {
    if (!tick || !candleRef.current) return
    const bars = barsRef.current
    if (!bars.length) return
    const last = bars[bars.length - 1]
    const updated = {
      time: last.time,
      open: last.open,
      high: Math.max(last.high, tick.price),
      low: Math.min(last.low, tick.price),
      close: tick.price,
    }
    bars[bars.length - 1] = { ...last, ...updated }
    candleRef.current.update(updated)
    // 마지막 봉 변동으로 SMA 끝점도 갱신
    MA_LINES.forEach((m, i) => {
      const series = sma(bars, m.period)
      if (series.length) maRefs.current[i].update(series[series.length - 1])
    })
  }, [tick])

  return <div ref={containerRef} style={{ width: '100%' }} />
}
