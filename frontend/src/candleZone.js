// 대금 양봉 매매의 매입 구간 계산(화면 미리보기·경고용). 실제 판정은 서버 엔진
// (backend/app/strategy/big_candle_trade.py)이 한다.

// 서버 CandleTradeConfig 기본값과 같게 둔다(등록 전 경고에 쓴다).
export const DEFAULT_ZONE_MARGIN_PCT = 10
export const DEFAULT_ADD_DROP_PCT = 5

// 기준 캔들 {open, close}와 여백(몸통 대비 %)으로 구간 {low, high}. 양봉이 아니면 null.
export function zoneOf(base, marginPct) {
  const body = (base?.close || 0) - (base?.open || 0)
  if (!(base?.open > 0) || body <= 0) return null
  const m = body * marginPct / 100
  return { low: base.open + m, high: base.close - m }
}

// 2차 매수(구간 안 & 1차가 −addDropPct% 이하) 가능성.
//   1차 전: 1차가 구간 상단에서 체결되는 가장 유리한 경우에도 구간 안에서 내려갈 수 있는 폭
//          maxDropPct = (1 − 하단/상단)×100 이 조건보다 작으면 불가능.
//   1차 후: 실제 1차 매수가에서 구간 하단까지의 폭으로 판정.
export function secondLegCheck(zone, addDropPct, firstPrice = 0) {
  if (!zone || !(zone.high > 0)) return null
  const from = firstPrice > 0 ? firstPrice : zone.high
  const maxDropPct = Math.max(0, (1 - zone.low / from) * 100)
  return {
    afterFill: firstPrice > 0,
    maxDropPct,
    possible: maxDropPct >= addDropPct,
    // 2차가 가능하려면 1차가 이 가격 이상에서 체결돼야 한다.
    minFirstPrice: zone.low / (1 - addDropPct / 100),
  }
}
