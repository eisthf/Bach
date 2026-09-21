// 일봉 거래대금이 기준(150억원) 이상인 봉에 노란색 반투명 세로 띠를 깐다.
// 캔들·이평선 아래(zOrder bottom)에 그려 캔들을 가리지 않는다.
export const AMOUNT_STRIPE_MIN = 15_000_000_000

export function amountStripeTimes(bars, min = AMOUNT_STRIPE_MIN) {
  return bars.filter((bar) => Number(bar.amount || 0) >= min).map((bar) => bar.time)
}

class StripeRenderer {
  constructor(source) { this._source = source }

  draw(target) {
    const { chart, times } = this._source
    if (!chart || !times.length) return
    const timeScale = chart.timeScale()
    const width = timeScale.options().barSpacing
    target.useBitmapCoordinateSpace(({ context: ctx, bitmapSize, horizontalPixelRatio: hr }) => {
      ctx.save()
      ctx.fillStyle = 'rgba(255, 214, 0, 0.28)'
      for (const time of times) {
        const x = timeScale.timeToCoordinate(time)
        if (x === null) continue
        const left = Math.round((x - width / 2) * hr)
        const right = Math.round((x + width / 2) * hr)
        ctx.fillRect(left, 0, Math.max(1, right - left), bitmapSize.height)
      }
      ctx.restore()
    })
  }
}

export class AmountStripePrimitive {
  constructor() {
    this.times = []
    this.chart = null
    this._requestUpdate = null
    this._views = [{ renderer: () => new StripeRenderer(this), zOrder: () => 'bottom' }]
  }

  attached({ chart, requestUpdate }) {
    this.chart = chart
    this._requestUpdate = requestUpdate
  }

  detached() {
    this.chart = null
    this._requestUpdate = null
  }

  paneViews() { return this._views }

  setTimes(times) {
    this.times = times
    this._requestUpdate?.()
  }
}
