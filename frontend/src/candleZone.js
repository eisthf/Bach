// 대금 양봉 매매의 매입 구간·2차 조건가 계산(화면 미리보기·경고용). 실제 판정은 서버 엔진
// (backend/app/strategy/big_candle_trade.py — add_trigger_for)이 하며, 아래 식은 그와 같게 둔다.

// 서버 CandleTradeConfig 기본값과 같게 둔다(등록 전 경고에 쓴다).
export const DEFAULT_CANDLE_CONFIG = {
  zone_margin_pct: 10,
  add_mode: 'mixed',
  add_drop_pct: 5,
  add_gap_pct: 80,
  add_min_drop_pct: 2,
}

// 기준 캔들 {open, close}와 여백(몸통 대비 %)으로 구간 {low, high}. 양봉이 아니면 null.
export function zoneOf(base, marginPct) {
  const body = (base?.close || 0) - (base?.open || 0)
  if (!(base?.open > 0) || body <= 0) return null
  const m = body * marginPct / 100
  return { low: base.open + m, high: base.close - m }
}

// 2차 조건가 {price, basis}. basis 'fixed'=1차가 −u%, 'gap'=하단까지 갭의 k%.
//   mixed: 1차가~하단 여유가 u%를 넘으면 고정 −u%와 갭의 k% 중 더 깊은 가격, 아니면 2차 없음
//   gap:   갭의 k% 가격(하락폭 < 최소 하락률이면 2차 없음)
export function addTriggerFor(cfg, firstPrice, zone) {
  const p = firstPrice
  if (!(p > 0)) return null
  const fixed = p * (1 - cfg.add_drop_pct / 100)
  const low = zone ? zone.low : null
  let gap = null
  if (low !== null && p > low) {
    const g = p - (p - low) * cfg.add_gap_pct / 100
    if ((p - g) / p * 100 >= cfg.add_min_drop_pct) gap = g
  }
  if (cfg.add_mode === 'fixed') return { price: fixed, basis: 'fixed' }
  if (cfg.add_mode === 'gap') return gap !== null ? { price: gap, basis: 'gap' } : null
  if (low === null || p <= low || (p - low) / p * 100 <= cfg.add_drop_pct) return null
  const deep = p - (p - low) * cfg.add_gap_pct / 100
  return deep < fixed ? { price: deep, basis: 'gap' } : { price: fixed, basis: 'fixed' }
}

// 2차 매수 가능성.
//   1차 전: 1차가 구간 상단에서 체결되는 가장 유리한 경우로 판정
//   1차 후: 실제 1차 매수가로 판정
// maxDropPct = 판정 기준가에서 구간 하단까지 내려갈 수 있는 폭(%).
export function secondLegCheck(zone, cfg, firstPrice = 0) {
  if (!zone || !(zone.high > 0)) return null
  const from = firstPrice > 0 ? firstPrice : zone.high
  const trigger = addTriggerFor(cfg, from, zone)
  return {
    afterFill: firstPrice > 0,
    maxDropPct: Math.max(0, (1 - zone.low / from) * 100),
    trigger,
    dropPct: trigger ? (1 - trigger.price / from) * 100 : null,
    possible: !!trigger && trigger.price >= zone.low,
  }
}

// 2차 조건 설명(1차 체결 전).
export function addRuleText(cfg) {
  const fixed = `1차가 −${cfg.add_drop_pct}%`
  const gap = `1차가~하단 갭의 ${cfg.add_gap_pct}% 하락(최소 ${cfg.add_min_drop_pct}%)`
  if (cfg.add_mode === 'fixed') return fixed
  if (cfg.add_mode === 'gap') return gap
  return `1차가~하단 여유가 ${cfg.add_drop_pct}% 초과면 −${cfg.add_drop_pct}%와 그 갭의 ${cfg.add_gap_pct}% 하락 중 더 깊은 곳, 아니면 2차 없음`
}

// KRX 호가 단위(backend/app/pricing.py tick_size 와 같음). 차트에서 찍은 가격을 맞춘다.
export function tickSize(price) {
  for (const [ceiling, step] of [[2_000, 1], [5_000, 5], [20_000, 10], [50_000, 50], [200_000, 100], [500_000, 500]]) {
    if (price < ceiling) return step
  }
  return 1_000
}

export function roundToTick(price) {
  if (!(price > 0)) return 0
  const step = tickSize(price)
  return Math.round(price / step) * step
}
