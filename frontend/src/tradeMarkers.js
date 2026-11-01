// 체결 시각이 실제 봉 구간에 포함될 때만 표시한다. 야간/미수신 봉으로 옮기지 않는다.
// 유효한 체결마다 { time: 봉 시각, side, qty, price } 를 돌려준다.
export function tradePoints(bars, trades, interval) {
  const points = []
  for (const trade of trades) {
    if (!['buy', 'sell'].includes(trade.side) || !(trade.qty > 0) || !(trade.price > 0)) continue
    let low = 0, high = bars.length - 1, index = -1
    while (low <= high) {
      const mid = (low + high) >> 1
      if (bars[mid].time <= trade.time) { index = mid; low = mid + 1 } else high = mid - 1
    }
    if (index < 0 || trade.time >= bars[index].time + interval * 60) continue
    points.push({ time: bars[index].time, side: trade.side, qty: trade.qty, price: trade.price })
  }
  return points.sort((a, b) => a.time - b.time)
}

// 같은 봉·같은 방향 체결은 하나로 묶고 가격은 수량 가중 평균.
export function tradeGroups(bars, trades, interval) {
  const groups = new Map()
  for (const point of tradePoints(bars, trades, interval)) {
    const key = `${point.time}:${point.side}`
    const group = groups.get(key) || { time: point.time, side: point.side, qty: 0, amount: 0 }
    group.qty += point.qty
    group.amount += point.qty * point.price
    groups.set(key, group)
  }
  return [...groups.values()].map((group) => ({ ...group, price: group.amount / group.qty }))
}

export const TRADE_COLORS = { buy: '#d32f2f', sell: '#1565c0' }

export function tradeMarkers(bars, trades, interval) {
  return tradeGroups(bars, trades, interval).map((group) => ({
    time: group.time,
    position: group.side === 'buy' ? 'belowBar' : 'aboveBar',
    shape: group.side === 'buy' ? 'arrowUp' : 'arrowDown',
    color: TRADE_COLORS[group.side],
    text: `${group.side === 'buy' ? 'B' : 'S'} ${group.qty.toLocaleString('ko-KR')}주 · ${group.price.toLocaleString('ko-KR', { maximumFractionDigits: 2 })}원`,
  }))
}

// 일정 표시(무상증자 공시·권리락 안내·권리락일 등). events: [{at, text, color, position, shape}]
// at = 'YYYY-MM-DD'(그날 첫 봉) 또는 'YYYY-MM-DDTHH:MM'(KST, 그 시각이 든 봉 — 일봉이면 그날 봉).
// 봉 시각은 KST 벽시계를 UTC로 둔 epoch라 문자열을 'Z'로 읽으면 같은 축이 된다. 봉이 없는 날은 건너뛴다.
export function eventMarkers(bars, events, interval) {
  const out = []
  for (const e of events || []) {
    if (!e?.at || bars.length === 0) continue
    const day = e.at.slice(0, 10)
    let bar = null
    if (interval === 1440 || e.at.length <= 10) {
      bar = bars.find((b) => new Date(b.time * 1000).toISOString().slice(0, 10) === day)
    } else {
      const t = Date.parse(`${e.at.slice(0, 16)}:00Z`) / 1000
      for (let i = bars.length - 1; i >= 0; i--) {
        if (bars[i].time <= t) {
          if (t < bars[i].time + interval * 60) bar = bars[i]
          break
        }
      }
    }
    if (!bar) continue
    out.push({ time: bar.time, position: e.position || 'aboveBar', shape: e.shape || 'arrowDown',
      color: e.color, text: e.text, size: e.size || 1 })
  }
  return out
}

// 스크리너 기준봉(조회일) 표시. 봉 시각은 KST 벽시계를 UTC로 둔 epoch라 ISO 날짜가 곧 KST 날짜다.
export const ANCHOR_COLOR = '#7b1fa2'

export function anchorMarker(bars, anchorDate) {
  if (!anchorDate) return null
  const bar = bars.find((b) => new Date(b.time * 1000).toISOString().slice(0, 10) >= anchorDate)
  return bar ? { time: bar.time, position: 'aboveBar', shape: 'arrowDown', color: ANCHOR_COLOR, size: 2, text: '기준봉' } : null
}
