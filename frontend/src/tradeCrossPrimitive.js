// 캔들 안쪽, 체결 건마다 실제 체결가 위치에 X를 그리는 시리즈 프리미티브.
// lightweight-charts v4 마커는 봉 위/아래(aboveBar/belowBar)에만 붙고 X 모양도
// 없어서, 체결가 좌표에 직접 그린다. 화살표·라벨 마커와 함께 쓴다.
import { TRADE_COLORS } from './tradeMarkers'

class CrossRenderer {
  constructor(source) { this._source = source }

  draw(target) {
    const { chart, series, points } = this._source
    if (!chart || !series || !points.length) return
    const timeScale = chart.timeScale()
    // 봉 간격에 맞춰 크기 조절 — 축소해도 이웃 봉을 덮지 않게.
    const half = Math.max(3, Math.min(6, timeScale.options().barSpacing * 0.3))
    target.useBitmapCoordinateSpace(({ context: ctx, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
      ctx.save()
      ctx.lineCap = 'round'
      for (const point of points) {
        const x = timeScale.timeToCoordinate(point.time)
        const y = series.priceToCoordinate(point.price)
        if (x === null || y === null) continue
        const cx = x * hr, cy = y * vr, dx = half * hr, dy = half * vr
        ctx.beginPath()
        ctx.moveTo(cx - dx, cy - dy); ctx.lineTo(cx + dx, cy + dy)
        ctx.moveTo(cx + dx, cy - dy); ctx.lineTo(cx - dx, cy + dy)
        // 흰 외곽선을 먼저 깔아 캔들 테두리·이평선 위에서도 보이게.
        ctx.strokeStyle = '#ffffff'
        ctx.lineWidth = 4 * hr
        ctx.stroke()
        ctx.strokeStyle = TRADE_COLORS[point.side]
        ctx.lineWidth = 2 * hr
        ctx.stroke()
      }
      ctx.restore()
    })
  }
}

export class TradeCrossPrimitive {
  constructor() {
    this.points = []
    this.chart = null
    this.series = null
    this._requestUpdate = null
    this._views = [{ renderer: () => new CrossRenderer(this), zOrder: () => 'top' }]
  }

  attached({ chart, series, requestUpdate }) {
    this.chart = chart
    this.series = series
    this._requestUpdate = requestUpdate
  }

  detached() {
    this.chart = null
    this.series = null
    this._requestUpdate = null
  }

  paneViews() { return this._views }

  setPoints(points) {
    this.points = points
    this._requestUpdate?.()
  }
}
