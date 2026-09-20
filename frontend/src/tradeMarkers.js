// 체결 시각이 실제 봉 구간에 포함될 때만 표시한다. 야간/미수신 봉으로 옮기지 않는다.
export function tradeMarkers(bars, trades, interval) {
  const groups = new Map()
  for (const trade of trades) {
    if (!['buy', 'sell'].includes(trade.side) || !(trade.qty > 0) || !(trade.price > 0)) continue
    let low = 0, high = bars.length - 1, index = -1
    while (low <= high) {
      const mid = (low + high) >> 1
      if (bars[mid].time <= trade.time) { index = mid; low = mid + 1 } else high = mid - 1
    }
    if (index < 0 || trade.time >= bars[index].time + interval * 60) continue
    const time = bars[index].time
    const key = `${time}:${trade.side}`
    const group = groups.get(key) || { time, side: trade.side, qty: 0, amount: 0 }
    group.qty += trade.qty
    group.amount += trade.qty * trade.price
    groups.set(key, group)
  }
  return [...groups.values()].sort((a, b) => a.time - b.time).map((group) => ({
    time: group.time,
    position: group.side === 'buy' ? 'belowBar' : 'aboveBar',
    shape: group.side === 'buy' ? 'arrowUp' : 'arrowDown',
    color: group.side === 'buy' ? '#d32f2f' : '#1565c0',
    text: `${group.side === 'buy' ? 'B' : 'S'} ${group.qty.toLocaleString('ko-KR')}주 · ${(group.amount / group.qty).toLocaleString('ko-KR', { maximumFractionDigits: 2 })}원`,
  }))
}
