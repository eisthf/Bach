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
