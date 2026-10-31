// 대금 양봉 매매·구간 매매 카드(같은 엔진). item.kind 로 갈린다.
//   candle: 매입 구간 = 기준 캔들 몸통 양 끝에서 몸통의 x%씩 안쪽(자동)
//   zone:   매입 구간 상단·하단을 사람이 지정 — 차트를 눌러 수평선으로 찍거나 숫자로 입력
// 2차 기준·익절(절반+트레일링 / 전량)·구간 이탈 손절은 둘 다 같다.
import React, { useEffect, useMemo, useState } from 'react'
import { useStore } from '../store'
import Chart from './Chart'
import PriceTicker from './PriceTicker'
import { useChartInterval } from '../chartPrefs'
import { ROUTES, navigate } from '../router'
import { addRuleText, projectedAverage, roundToTick, secondLegCheck, zoneOf } from '../candleZone'

const fmt = (n) => Number(n || 0).toLocaleString('ko-KR')
const fmtPrice = (n) => (n ? Math.round(n).toLocaleString('ko-KR') : '—')

const PHASES = {
  DRAFT: { label: '설정 중', tone: 'idle' },
  WATCHING: { label: '매입 구간 대기', tone: 'wait' },
  ACCUMULATING: { label: '1차 보유 · 2차 대기', tone: 'live' },
  HOLDING: { label: '익절 대기', tone: 'live' },
  TRAILING: { label: '트레일링 스톱', tone: 'live' },
  DONE: { label: '종료', tone: 'end' },
  MANUAL: { label: '수동 전환됨', tone: 'end' },
}
export const ACTIVE = ['WATCHING', 'ACCUMULATING', 'HOLDING', 'TRAILING']
const HOLDING = ['ACCUMULATING', 'HOLDING', 'TRAILING']
const WATCH_MINUTES = [1, 3, 5, 10, 15, 30, 60]
const LEG_STATUS = { failed: '실패·중단', canceled: '취소(익절)' }

const toForm = (cfg) => ({ ...cfg })

function fromForm(f) {
  return {
    total_krw: Math.round(Number(f.total_krw) || 0),
    first_ratio_pct: Number(f.first_ratio_pct),
    zone_margin_pct: Number(f.zone_margin_pct),
    add_mode: f.add_mode,
    add_drop_pct: Number(f.add_drop_pct),
    add_gap_pct: Number(f.add_gap_pct),
    add_min_drop_pct: Number(f.add_min_drop_pct),
    take_profit_pct: Number(f.take_profit_pct),
    take_profit_mode: f.take_profit_mode || 'pct',
    take_profit_line: Number(f.take_profit_line) || 0,
    trailing: f.trailing !== false,
    trailing_pct: Number(f.trailing_pct),
    stop_margin_pct: Number(f.stop_margin_pct),
    stop_mode: f.stop_mode || 'close',
    entry_days: Math.round(Number(f.entry_days) || 0),
    watch_mode: f.watch_mode,
    watch_minutes: Number(f.watch_minutes),
  }
}

function validate(c) {
  if (c.total_krw < 10_000) return '총 투자액은 1만원 이상이어야 합니다.'
  if (!(c.first_ratio_pct >= 10 && c.first_ratio_pct <= 90)) return '1차 매수 비중은 10~90%여야 합니다.'
  if (!(c.zone_margin_pct >= 0 && c.zone_margin_pct < 50)) return '매입 구간 여백은 0 이상 50% 미만이어야 합니다.'
  if (!(c.add_drop_pct > 0 && c.add_drop_pct < 100)) return '2차 매수 하락률은 0~100% 사이여야 합니다.'
  if (!(c.add_gap_pct > 0 && c.add_gap_pct <= 100)) return '2차 구간 폭 비율은 0 초과 100% 이하여야 합니다.'
  if (!(c.add_min_drop_pct >= 0 && c.add_min_drop_pct < 100)) return '2차 최소 하락률은 0~100% 사이여야 합니다.'
  if (c.take_profit_mode === 'line' ? !(c.take_profit_line > 0) : !(c.take_profit_pct > 0 && c.take_profit_pct <= 500)) {
    return c.take_profit_mode === 'line' ? '익절 라인 가격을 정하세요(입력·차트에서 찍기).' : '익절 상승률은 0~500% 사이여야 합니다.'
  }
  if (c.trailing && !(c.trailing_pct > 0 && c.trailing_pct < 100)) return '트레일링 하락률은 0~100% 사이여야 합니다.'
  if (!(c.stop_margin_pct >= 0 && c.stop_margin_pct < 50)) return '구간 이탈 손절 여유는 0~50% 사이여야 합니다.'
  if (!(c.entry_days >= 1 && c.entry_days <= 60)) return '1차 매수 기한은 1~60거래일이어야 합니다.'
  return ''
}

export default function CandleTradeCard({ account, item }) {
  const { ticks, candlePositions, logs, actions } = useStore()
  const prefKey = `candle:${account.id}:${item.code}`
  const [interval, setInterval] = useChartInterval(prefKey)
  const [form, setForm] = useState(() => toForm(item.config))
  const [dirty, setDirty] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const tick = (ticks[account.id] || {})[item.code]
  const pos = (candlePositions[account.id] || {})[item.code]
  const ph = PHASES[item.phase] || { label: item.phase, tone: 'idle' }
  const draft = item.phase === 'DRAFT'
  const active = ACTIVE.includes(item.phase)
  const holding = HOLDING.includes(item.phase)
  const editable = draft || active
  const bought = item.legs.some((l) => l.status === 'filled')
  const isZone = item.kind === 'zone'
  const label = isZone ? '구간매매' : '대금양봉'
  // 구간 매매: 상단·하단 입력(차트 클릭 또는 숫자). pick = 'high' | 'low' | null
  const [zoneForm, setZoneForm] = useState(() => ({ low: item.zone?.low || '', high: item.zone?.high || '' }))
  const [zoneDirty, setZoneDirty] = useState(false)
  const [pick, setPick] = useState(null)
  // 새 구간(재진입) 편집 중. 보유 중·익절 전에만(rezone_allowed). budget = 새 회차 매수 금액.
  const [rezoning, setRezoning] = useState(false)
  const [budget, setBudget] = useState('')
  const zoneEditable = item.zone_editable || (rezoning && item.rezone_allowed)
  // 새 구간 회차(2회차~): 총 투자액 대신 '이월분 매입 + 이번 회차 매수 금액'을 보여 준다.
  const round = item.round || { no: 1 }
  const laterRound = round.no > 1
  const [roundBudget, setRoundBudget] = useState(String(round.budget || ''))
  const [roundDirty, setRoundDirty] = useState(false)
  useEffect(() => { if (!roundDirty) setRoundBudget(String(round.budget || '')) }, [round.budget, roundDirty])
  const roundBudgetNum = Math.round(Number(roundBudget) || 0)
  const roundEditable = laterRound && item.legs.every((l) => l.status === 'waiting')
  // 이번 회차에 체결이 없으면 [새 구간 시작]은 새 회차가 아니라 이번 회차의 구간·금액 변경이다(서버와 같은 규칙).
  const sameRound = laterRound && !item.legs.some((l) => l.status === 'filled')
  useEffect(() => { if (!item.rezone_allowed) setRezoning(false) }, [item.rezone_allowed])
  useEffect(() => {
    if (!zoneDirty) setZoneForm({ low: item.zone?.low || '', high: item.zone?.high || '' })
  }, [item.zone?.low, item.zone?.high, zoneDirty])
  const zLow = Number(zoneForm.low)
  const zHigh = Number(zoneForm.high)
  const budgetNum = Math.round(Number(budget) || 0)
  const zoneFormError = rezoning && !(budgetNum >= 10_000) ? '새 구간 매수 금액은 1만원 이상이어야 합니다.'
    : !isZone || !zoneDirty ? ''
    : !(zLow > 0 && zHigh > 0) ? '상단·하단을 모두 정하세요.'
      : !(zLow < zHigh) ? '하단은 상단보다 낮아야 합니다.' : ''
  const setZoneAt = (key, value) => { setZoneForm((z) => ({ ...z, [key]: value })); setZoneDirty(true) }
  // 상단·하단 선 끌기(마우스·터치). 놓으면 입력값이 바뀌고, [구간 저장]으로 확정한다.
  const onLineDrag = (id, p) => (id === 'tp' ? set('take_profit_line', roundToTick(p)) : setZoneAt(id, roundToTick(p)))
  const onPriceClick = pick === 'tp' ? (p) => { set('take_profit_line', roundToTick(p)); setPick(null) }
    : pick && zoneEditable ? (p) => {
    setZoneAt(pick, roundToTick(p))
    // 상단 다음엔 하단을 바로 찍을 수 있게(비어 있을 때만).
    setPick(pick === 'high' && !zoneForm.low ? 'low' : null)
  } : null

  // 서버 설정이 바뀌면(다른 화면·저장 완료) 편집 중이 아닐 때만 폼을 맞춘다.
  useEffect(() => { if (!dirty) setForm(toForm(item.config)) }, [item.config, dirty])

  const cfg = fromForm(form)
  const formError = validate(cfg)
  // 저장 전 편집값으로 구간을 미리 계산한다 — 차트·표에 바로 반영.
  //   대금 양봉: 기준 캔들 + (편집 중) 여백,  구간 매매: 입력 중인 상단·하단
  const zone = isZone
    ? (zoneDirty && !zoneFormError ? { low: zLow, high: zHigh } : item.zone)
    : ((dirty && !bought ? zoneOf(item.base, cfg.zone_margin_pct) : item.zone) || item.zone)
  // 2차 매수 가능성. 2차가 아직 대기 중이고 운용(또는 설정) 중일 때만 경고한다.
  const leg2Waiting = item.legs[1]?.status === 'waiting'
  const leg2 = secondLegCheck(zone, cfg, bought ? item.legs[0].price : 0)
  const leg2Warn = leg2 && !leg2.possible && leg2Waiting && editable
  const body = item.base.close - item.base.open

  const startRezone = () => {
    setRezoning(true)
    setBudget(item.pending_amount > 0 ? String(item.pending_amount) : '')
    setPick('high')
  }
  const cancelRezone = () => {
    setRezoning(false)
    setZoneDirty(false)
    setPick(null)
  }
  const saveRezone = async () => {
    const message = `${account.danger ? '⚠️ 실전 ' : ''}${account.label} 계좌 구간 매매 새 구간\n${item.name} ${item.code}\n`
      + `새 매입 구간 ${fmtPrice(zLow)} ~ ${fmtPrice(zHigh)}, 매수 ${fmt(budgetNum)}원(1차·2차)\n`
      + (sameRound ? `${round.no}회차(체결 전)의 구간·금액을 바꿉니다(회차 유지). ` : `${round.no + 1}회차로 시작합니다. 대기 중인 차수는 취소되고, `)
      + `보유 ${fmt(item.shares)}주(평단 ${fmtPrice(item.avg_cost)})는 그대로 이어집니다.\n`
      + `익절은 합산 평단, 손절은 새 구간 하단 기준입니다. 진행할까요?`
    const r = await run(() => actions.rezoneCandle(account.id, item.code, zLow, zHigh, budgetNum, message), '새 구간 시작됨')
    if (r) cancelRezone()
    return r
  }

  const saveRoundBudget = async () => {
    const r = await run(() => actions.setRoundBudget(account.id, item.code, roundBudgetNum), '회차 금액 저장됨')
    if (r) setRoundDirty(false)
    return r
  }

  const saveZone = async () => {
    const r = await run(() => actions.setCandleZone(account.id, item.code, zLow, zHigh), '구간 저장됨')
    if (r) { setZoneDirty(false); setPick(null) }
    return r
  }

  const set = (key, value) => { setForm((f) => ({ ...f, [key]: value })); setDirty(true) }

  const run = async (fn, okText) => {
    setBusy(true)
    setMsg('')
    try {
      const r = await fn()
      if (r !== null && okText) setMsg(okText)
      return r
    } catch (e) {
      setMsg(`실패: ${e.message || e}`)
      return null
    } finally {
      setBusy(false)
    }
  }

  const save = async () => {
    const r = await run(() => actions.setCandleConfig(account.id, item.code, cfg), '설정 저장됨')
    if (r) setDirty(false)
    return r
  }

  const start = async () => {
    if (dirty && !(await save())) return
    if (isZone && zoneDirty && !(await saveZone())) return
    const first = Math.floor(cfg.total_krw * cfg.first_ratio_pct / 100)
    const message = `⚠️ ${account.label} 계좌 ${isZone ? '구간' : '대금 양봉'} 매매 감시 시작\n${item.name} ${item.code}\n`
      + `매입 구간 ${fmtPrice(zone?.low)} ~ ${fmtPrice(zone?.high)}에 들어오면 1차 ${fmt(first)}원 시장가 매수가 자동으로 나갑니다.\n진행할까요?`
    await run(() => actions.startCandle(account.id, item.code, message), '감시 시작됨')
  }

  const myLogs = logs.filter((l) => l.account === account.id && l.text.startsWith(`[${item.code}] ${label}`)).slice(-8)
  const price = tick?.price
  const pnlPct = item.shares > 0 && item.avg_cost > 0 && price ? (price - item.avg_cost) / item.avg_cost * 100 : null
  const inZone = zone && price ? price >= zone.low && price <= zone.high : null

  // 익절 라인 방식이면 편집 중인 값을 차트에 그리고 끌 수 있게 한다(저장은 [설정 저장]).
  const tpLine = form.take_profit_mode === 'line' && Number(form.take_profit_line) > 0 ? Number(form.take_profit_line) : 0
  const tpTitle = form.trailing === false ? '익절(전량)' : '절반 익절'
  const lines = useMemo(() => {
    const out = []
    if (isZone) {
      // 입력값 그대로(하나만 찍었거나 끄는 중 상·하단이 뒤바뀐 상태도 보여 준다). 저장 전엔 점선.
      if (zHigh > 0) out.push({ price: zHigh, color: '#2e7d32', title: '구간 상단', dashed: zoneDirty, dragId: 'high' })
      if (zLow > 0) out.push({ price: zLow, color: '#2e7d32', title: '구간 하단', dashed: zoneDirty, dragId: 'low' })
    } else if (zone) {
      out.push({ price: zone.high, color: '#2e7d32', title: '구간 상단' })
      out.push({ price: zone.low, color: '#2e7d32', title: '구간 하단' })
    }
    if (item.legs[1]?.trigger && item.legs[1].possible) out.push({ price: item.legs[1].trigger, color: '#6a1b9a', title: '2차 ≤', dashed: true })
    if (item.avg_cost > 0 && holding) out.push({ price: item.avg_cost, color: '#555', title: '평단', dashed: true })
    if (tpLine) out.push({ price: tpLine, color: '#d32f2f', title: `${tpTitle} 라인`, dashed: dirty, dragId: 'tp' })
    else if (item.take_profit_price) out.push({ price: item.take_profit_price, color: '#d32f2f', title: item.config.trailing === false ? '익절(전량)' : '절반 익절', dashed: true })
    if (item.trailing_stop) out.push({ price: item.trailing_stop, color: '#e65100', title: '트레일링', dashed: true })
    // 손절선은 하단과 다를 때만(여유 0이면 구간 하단선이 곧 손절선).
    if (holding && item.stop_line && zone && item.stop_line < zone.low) out.push({ price: item.stop_line, color: '#1565c0', title: '손절선', dashed: true })
    return out
  }, [zone?.low, zone?.high, isZone, zoneDirty, zLow, zHigh, item.legs, item.avg_cost, item.take_profit_price, item.trailing_stop, item.config.trailing, item.stop_line, holding, tpLine, tpTitle, dirty])

  return (
    <div className={`stock-panel candle-card tone-${ph.tone}`}>
      <div className="panel-header">
        <div className="panel-id">
          <span className="panel-code">{item.code}</span>
          <span className="panel-name">{item.name}</span>
        </div>
        <PriceTicker tick={tick} />
        <div className="panel-actions">
          <span className={`account-badge ${account.danger ? 'danger' : 'safe'}`}>{account.label}</span>
          <span className={`ct-phase ct-${ph.tone}`}>{ph.label}</span>
        </div>
      </div>

      {item.notice && <div className="recovery-notice" role="alert">{item.notice}</div>}
      {leg2Warn && (
        <div className="recovery-notice" role="alert">
          {leg2.afterFill
            ? <>⚠️ 2차 매수가 불가능합니다 — 1차 매수가 {fmtPrice(item.legs[0].price)}에서 구간 하단 {fmtPrice(zone.low)}까지
              {' '}{leg2.maxDropPct.toFixed(1)}%뿐이라 2차 조건({addRuleText(cfg)})을 만족하는 가격이 구간 안에 없습니다.</>
            : <>⚠️ 이 {isZone ? '구간은' : '캔들은'} 2차 매수가 불가능합니다 — 구간 안 최대 하락률 {leg2.maxDropPct.toFixed(1)}%로는
              {' '}2차 조건({addRuleText(cfg)})을 만족할 수 없습니다. 1차만 매수됩니다.</>}
        </div>
      )}
      {item.exit_reason && !active && <div className="ct-exit">종료 사유: {item.exit_reason}</div>}

      <div className="ct-summary">
        {!isZone && (
          <span title="조회일의 기준 캔들">기준 캔들 <strong>{item.base.date}</strong>{' '}
            {fmtPrice(item.base.open)} → {fmtPrice(item.base.close)}
            {!item.base.confirmed && <span className="muted" title="다음 거래일 감시 시작 때 일봉 확정값으로 다시 맞춥니다"> (등록값)</span>}
          </span>
        )}
        <span>매입 구간 <strong className={inZone ? 'up' : ''}>{zone ? `${fmtPrice(zone.low)} ~ ${fmtPrice(zone.high)}` : '—'}</strong>
          {inZone && <span className="muted"> · 현재가 구간 안</span>}</span>
        {active && item.day && (
          <span className="muted">
            {isZone ? `감시 ${item.days_elapsed}거래일째(${item.watch_from}부터)`
              : item.days_elapsed > 0 ? `기준일 이후 ${item.days_elapsed}거래일째` : '기준일 당일(감시는 다음 거래일부터)'}
            {item.phase === 'WATCHING' && item.days_elapsed > 0 && ` / 기한 ${item.config.entry_days}거래일`}
          </span>
        )}
        {item.stop_armed && <span className="down">전일 종가 {fmtPrice(item.prev_close)} &lt; 손절선 {fmtPrice(item.stop_line)} — 오늘 그 아래에서 전량 매도</span>}
      </div>
      {(bought || item.shares > 0) && (
        <div className="ct-summary">
          <span>보유 <strong>{fmt(item.shares)}주</strong></span>
          <span>평단 <strong>{fmtPrice(item.avg_cost)}</strong></span>
          <span>평가 <strong className={pnlPct > 0 ? 'up' : pnlPct < 0 ? 'down' : ''}>
            {pnlPct === null ? '—' : `${pnlPct > 0 ? '+' : ''}${pnlPct.toFixed(2)}%`}</strong></span>
          {item.round?.no > 1 && (
            <span className="muted" title="새 구간 설정으로 앞 회차에서 이어 온 보유">
              {item.round.no}회차 · 이월 {fmt(item.round.carry_qty)}주 @ {fmtPrice(item.round.carry_avg)}
            </span>
          )}
          {item.sold_qty > 0 && <span className="muted">매도 {fmt(item.sold_qty)}주</span>}
          {item.phase === 'TRAILING' && <span>고점 <strong>{fmtPrice(item.peak)}</strong> · 스톱 <strong>{fmtPrice(item.trailing_stop)}</strong></span>}
          {pos && <span className="muted">계좌 잔고 {fmt(pos.quantity)}주</span>}
        </div>
      )}

      <div className="interval-selector ct-intervals">
        {[1440, 3].map((v) => (
          <button key={v} className={`iv-btn${interval === v ? ' active' : ''}`} aria-pressed={interval === v}
            onClick={() => setInterval(v)}>{v === 1440 ? '일봉' : '3분봉'}</button>
        ))}
      </div>
      {isZone && (
        <div className="ct-zone">
          <strong>{rezoning ? '새 매입 구간' : '매입 구간'}</strong>
          <label>상단 <input type="number" min="1" step="any" value={zoneForm.high} disabled={!zoneEditable || busy}
            onChange={(e) => setZoneAt('high', e.target.value)} /></label>
          <button className={pick === 'high' ? 'on' : ''} disabled={!zoneEditable || busy}
            aria-pressed={pick === 'high'} onClick={() => setPick(pick === 'high' ? null : 'high')}>차트에서 상단 찍기</button>
          <label>하단 <input type="number" min="1" step="any" value={zoneForm.low} disabled={!zoneEditable || busy}
            onChange={(e) => setZoneAt('low', e.target.value)} /></label>
          <button className={pick === 'low' ? 'on' : ''} disabled={!zoneEditable || busy}
            aria-pressed={pick === 'low'} onClick={() => setPick(pick === 'low' ? null : 'low')}>차트에서 하단 찍기</button>
          {rezoning && (
            <label title="새 구간에서 살 금액(1차·2차를 매매 설정의 1차 비중대로 나눔). 기본값은 취소될 대기 차수 금액입니다.">
              매수 금액 <input type="number" min="10000" step="10000" value={budget} disabled={busy}
                onChange={(e) => setBudget(e.target.value)} />원
            </label>
          )}
          {rezoning && (
            <>
              {/* 구간을 그대로 두고 금액만 바꿔 새로 시작해도 된다 — 구간 값이 올바르면 켠다. */}
              <button className="on" disabled={busy || !(zLow > 0 && zLow < zHigh) || !!zoneFormError} onClick={saveRezone}>새 구간 시작</button>
              <button disabled={busy} onClick={cancelRezone}>취소</button>
            </>
          )}
          {!rezoning && item.rezone_allowed && (
            <button disabled={busy} onClick={startRezone}
              title="대기 중인 차수를 취소하고, 지금 보유는 그대로 둔 채 새 구간에서 새로 1차·2차 매수합니다.">새 구간 설정</button>
          )}
          {/* 항상 자리를 차지하게 둔다 — 선을 끄는 도중에 버튼이 나타나며 차트가 밀리면 끌기가 튄다. */}
          {item.zone_editable && (
            <button className={zoneDirty ? 'on' : ''} disabled={busy || !zoneDirty || !!zoneFormError} onClick={saveZone}>
              {zoneDirty ? '구간 저장' : '저장됨'}
            </button>
          )}
          <span className={`ct-zone-hint ${zoneFormError ? 'down' : 'muted'}`}>
            {zoneFormError
              || (rezoning ? (sameRound
                ? `${round.no}회차는 아직 체결이 없어 회차를 유지한 채 구간·금액만 바꿉니다. 보유 ${fmt(item.shares)}주(평단 ${fmtPrice(item.avg_cost)}) 그대로, 익절은 합산 평단, 손절은 새 하단 기준.`
                : `대기 차수 ${fmt(item.pending_amount)}원은 취소, 보유 ${fmt(item.shares)}주(평단 ${fmtPrice(item.avg_cost)})는 이어 가고 ${round.no + 1}회차로 새 구간에서 새로 1차·2차 매수합니다. 익절은 합산 평단, 손절은 새 하단 기준.`)
                : !item.zone_editable ? (item.rezone_allowed ? '1차 매수 후에는 [새 구간 설정]으로만 구간을 바꿀 수 있습니다(보유 이월).' : '1차 매수 후에는 구간을 바꿀 수 없습니다.')
                : pick && pick !== 'tp' ? `차트에서 ${pick === 'high' ? '상단' : '하단'} 가격을 누르세요(호가 단위로 맞춤).`
                  : zone ? `폭 ${((1 - zone.low / zone.high) * 100).toFixed(1)}% — 차트의 구간 선을 끌어 옮길 수 있습니다.`
                    : '상단·하단 가격을 차트에서 찍거나 직접 입력하세요.')}
          </span>
        </div>
      )}
      <Chart prefKey={prefKey} account={account.id} code={item.code} interval={interval} tick={tick} height={isZone ? 320 : 260}
        anchorDate={isZone ? null : item.base.date} priceLines={lines} onPriceClick={onPriceClick}
        draggableLines={[
          ...(isZone && zoneEditable ? [{ id: 'high', price: zHigh }, { id: 'low', price: zLow }] : []),
          ...(tpLine && editable ? [{ id: 'tp', price: tpLine }] : []),
        ]}
        onLineDrag={(isZone && zoneEditable) || (tpLine && editable) ? onLineDrag : null} />

      <div className="panels">
        <div className="ct-config">
          <div className="panel-title">매매 설정</div>
          <fieldset disabled={!editable || busy}>
            {!laterRound ? (
              <label>총 투자액
                <input type="number" min="10000" step="10000" value={form.total_krw}
                  onChange={(e) => set('total_krw', e.target.value)} />원
              </label>
            ) : (
              <>
                <div className="ct-round-total" title="이 종목에 들어갔거나 들어갈 돈: 앞 회차에서 이어 온 보유의 실제 매입액 + 이번 회차 매수 계획">
                  투입 예정 총액 <strong>{fmt(Math.round(round.carry_cost + (roundDirty ? roundBudgetNum : round.budget)))}원</strong>
                  <span className="muted"> = 이월분 매입 {fmt(Math.round(round.carry_cost))}원
                    ({fmt(round.carry_qty)}주 @ {fmtPrice(round.carry_avg)}) + 이번 회차 {fmt(roundDirty ? roundBudgetNum : round.budget)}원</span>
                </div>
                <label title="새 구간에서 살 금액. 1차 비중대로 1차·2차로 나눕니다. 이번 회차 1차를 사기 전까지만 바꿀 수 있습니다.">
                  {round.no}회차 매수 금액
                  <input type="number" min="10000" step="10000" value={roundBudget} disabled={!roundEditable}
                    onChange={(e) => { setRoundBudget(e.target.value); setRoundDirty(true) }} />원
                  <span className="muted">(1차 {fmt(item.legs[0]?.amount)} · 2차 {fmt(item.legs[1]?.amount)})</span>
                  {roundEditable && roundDirty && (
                    <button type="button" disabled={busy || roundBudgetNum < 10_000} onClick={saveRoundBudget}>금액 저장</button>
                  )}
                </label>
              </>
            )}
            <label title="1차 매수에 쓰는 비중. 나머지가 2차 매수 금액입니다.">1차 매수 비중
              <input type="number" min="10" max="90" step="any" value={form.first_ratio_pct} disabled={bought}
                onChange={(e) => set('first_ratio_pct', e.target.value)} />%
              <span className="muted">(2차 {Math.max(0, 100 - Number(form.first_ratio_pct || 0))}%)</span>
            </label>
            {!isZone && (
              <label title={`몸통 ${fmtPrice(body)}원의 이 비율만큼 시가 위·종가 아래를 구간 끝으로 잡습니다.`}>매입 구간: 몸통 양 끝에서
                <input type="number" min="0" max="49" step="any" value={form.zone_margin_pct} disabled={bought}
                  onChange={(e) => set('zone_margin_pct', e.target.value)} />% 안쪽
              </label>
            )}
            {!laterRound && <label title={`${isZone ? '감시를 시작한 거래일을 1일째로' : '기준일 다음 거래일부터'} 셉니다. 이 안에 구간 진입이 없으면 매매하지 않고 종료합니다.`}>1차 매수 기한: {isZone ? '감시 시작부터' : '기준일 이후'}
              <input type="number" min="1" max="60" step="1" value={form.entry_days} disabled={bought}
                onChange={(e) => set('entry_days', e.target.value)} />거래일
            </label>}
            <label title="1차 매수 다음 날부터, 가격이 매입 구간 안이면서 2차 매수가 이하로 내려오면 2차를 삽니다. 혼합: ①1차가 대비 최소 하락과 ②구간 상단에서 구간 폭의 일정 비율 아래 중 더 낮은 가격. 1차가가 하단에 너무 가까우면 2차를 하지 않습니다.">2차 매수 가격
              <select value={form.add_mode} onChange={(e) => set('add_mode', e.target.value)}>
                <option value="mixed">혼합</option>
                <option value="fixed">1차가 대비 하락률</option>
                <option value="gap">구간 폭 비율</option>
              </select>
            </label>
            {form.add_mode === 'fixed' && (
              <label className="ct-sub" title="2차 조건가 = 1차 매수가 × (1 − 이 비율)">2차: 1차 매수가 대비
                <input type="number" min="0.1" max="99" step="any" value={form.add_drop_pct}
                  onChange={(e) => set('add_drop_pct', e.target.value)} />% 하락
              </label>
            )}
            {form.add_mode === 'mixed' && (
              <label className="ct-sub" title="① = 1차 매수가 × (1 − 이 비율). 1차가에서 구간 하단까지가 이 비율보다 가까우면(구간 안에서 그만큼 못 내려가면) 2차를 하지 않습니다.">
                ① 1차가보다
                <input type="number" min="0.1" max="99" step="any" value={form.add_drop_pct}
                  onChange={(e) => set('add_drop_pct', e.target.value)} />% 아래 (하단이 이보다 가까우면 2차 없음)
              </label>
            )}
            {form.add_mode !== 'fixed' && (
              <label className="ct-sub" title="2차 가격 = 구간 상단 − (상단 − 하단) × 이 비율. 1차가와 무관하게 구간으로 정해진다. 100%면 하단.">
                {form.add_mode === 'mixed' ? '② ' : '2차: '}구간 상단에서 구간 폭의
                <input type="number" min="1" max="100" step="any" value={form.add_gap_pct}
                  onChange={(e) => set('add_gap_pct', e.target.value)} />% 아래
                {Number(form.add_gap_pct) > 0 && Number(form.add_gap_pct) <= 100 && ` (하단에서 ${+(100 - Number(form.add_gap_pct)).toFixed(2)}% 위)`}
                {form.add_mode === 'mixed' ? ' — ①·② 중 낮은 가격에서 2차' : ''}
              </label>
            )}
            {form.add_mode === 'gap' && (
              <>
                <label className="ct-sub" title="1차가 2차 가격 근처나 그 아래에서 체결됐으면, 같은 가격에 금액만 두 배로 사지 않도록 2차를 하지 않습니다.">
                  1차가 대비 최소 하락률
                  <input type="number" min="0" max="99" step="any" value={form.add_min_drop_pct}
                    onChange={(e) => set('add_min_drop_pct', e.target.value)} />% (이보다 얕으면 2차 없음)
                </label>
              </>
            )}
            <label title={form.trailing !== false
              ? '도달하면 보유의 절반을 팔고, 아직 사지 않은 2차 계획은 취소합니다.'
              : '도달하면 보유 전량을 팔고 매매를 끝냅니다. 아직 사지 않은 2차 계획은 취소합니다.'}>
              {form.trailing !== false ? '절반 익절' : '전량 익절'}:
              <select value={form.take_profit_mode || 'pct'} onChange={(e) => set('take_profit_mode', e.target.value)}
                aria-label="익절 기준">
                <option value="pct">평단 대비</option>
                <option value="line">가격 라인</option>
              </select>
              {form.take_profit_mode === 'line' ? (
                <>
                  <input type="number" min="1" step="any" value={form.take_profit_line || ''}
                    onChange={(e) => set('take_profit_line', e.target.value)} />원 이상
                  <button type="button" className={pick === 'tp' ? 'on' : ''} aria-pressed={pick === 'tp'}
                    onClick={() => setPick(pick === 'tp' ? null : 'tp')}>차트에서 찍기</button>
                </>
              ) : (
                <><input type="number" min="0.1" max="500" step="any" value={form.take_profit_pct}
                  onChange={(e) => set('take_profit_pct', e.target.value)} />% 상승</>
              )}
            </label>
            {form.take_profit_mode === 'line' && (
              <div className={`ct-tp-hint ${tpLine && ((holding && item.avg_cost > 0 && tpLine <= item.avg_cost) || (price && tpLine <= price)) ? 'down' : 'muted'}`}>
                {pick === 'tp' ? '차트에서 익절 라인 가격을 누르세요(호가 단위로 맞춤). 차트의 익절 라인은 끌어서 옮길 수 있습니다.'
                  : !tpLine ? '익절 라인 가격을 입력하거나 차트에서 찍으세요.'
                    : holding && item.avg_cost > 0 && tpLine <= item.avg_cost ? `⚠️ 라인이 평단 ${fmtPrice(item.avg_cost)} 이하 — 손실을 보고 익절할 수 있습니다.`
                      : price && tpLine <= price ? `⚠️ 라인이 현재가 ${fmtPrice(price)} 이하 — 보유 중이면 저장 즉시 익절합니다.`
                        : `지금 평단 대비 ${holding && item.avg_cost > 0 ? `+${((tpLine / item.avg_cost - 1) * 100).toFixed(1)}%` : '—'} · 차트의 익절 라인을 끌어 옮길 수 있습니다(저장은 [설정 저장]).`}
                {tpLine > 0 && pick !== 'tp' && (() => {
                  const p = projectedAverage({ shares: item.shares, avgCost: item.avg_cost, legs: item.legs, zone, cfg, price })
                  if (!p) return null
                  const parts = [p.first && `1차 ≈ ${fmtPrice(p.first)}`, p.second ? `2차 ≈ ${fmtPrice(p.second)}` : p.secondNone && '2차 없음 — 1차만 반영']
                    .filter(Boolean).join(', ')
                  const pct = (tpLine / p.avg - 1) * 100
                  return (
                    <div className={pct <= 0 ? 'down' : 'muted'}
                      title="1차는 구간에 들어오는 가격(위에서 내려오면 상단), 2차는 2차 조건가로 어림했습니다. 실제 체결가와 다를 수 있습니다.">
                      {round.no > 1 ? `${round.no}회차 ` : ''}계획대로 다 사면({parts}) 평단 ≈ {fmtPrice(p.avg)} → 라인은 {pct > 0 ? '+' : ''}{pct.toFixed(1)}%
                    </div>
                  )
                })()}
              </div>
            )}
            <label title="켜면 절반 익절 뒤 나머지를 고점 대비 하락률로 따라가며 팝니다. 끄면 익절가에서 전량 매도합니다. 이미 트레일링 중이면 꺼도 끝까지 트레일링합니다.">
              <input type="checkbox" checked={form.trailing !== false}
                onChange={(e) => set('trailing', e.target.checked)} />
              트레일링 스톱{form.trailing !== false ? ': 고점 대비' : ' 안 함'}
              {form.trailing !== false && (
                <><input type="number" min="0.1" max="99" step="any" value={form.trailing_pct}
                  onChange={(e) => set('trailing_pct', e.target.value)} />% 하락</>
              )}
            </label>
            <label title="손절선 = 구간 하단 × (1 − 이 비율). 0이면 하단 그대로. 종가 확인: 전일 종가가 손절선 아래인 날만, 그날 손절선 아래에서 전량 매도(장중 일시 이탈은 무시). 즉시: 보유 중 손절선 아래 가격이 한 번이라도 나오면 바로 전량 매도.">
              구간 이탈 손절: 하단보다
              <input type="number" min="0" max="49" step="any" value={form.stop_margin_pct ?? 0}
                onChange={(e) => set('stop_margin_pct', e.target.value)} />% 아래,
              <select value={form.stop_mode || 'close'} onChange={(e) => set('stop_mode', e.target.value)}
                aria-label="구간 이탈 손절 방식">
                <option value="close">종가 확인</option>
                <option value="instant">즉시</option>
              </select>
            </label>
            <label title="틱: 체결마다 판단. 분: 완성된 N분봉의 종가로 판단.">가격 감시
              <select value={form.watch_mode} onChange={(e) => set('watch_mode', e.target.value)}>
                <option value="tick">틱 단위</option>
                <option value="minute">분 단위</option>
              </select>
              {form.watch_mode === 'minute' && (
                <select value={form.watch_minutes} onChange={(e) => set('watch_minutes', Number(e.target.value))}
                  aria-label="감시 분봉 길이">
                  {WATCH_MINUTES.map((m) => <option key={m} value={m}>{m}분봉</option>)}
                </select>
              )}
            </label>
          </fieldset>
          <p className="muted ct-rule">{(() => {
            const m = Number(form.stop_margin_pct) > 0 ? +Number(form.stop_margin_pct) : 0
            const line = m > 0 ? `매입 구간 하단의 ${m}% 아래(손절선)` : '매입 구간 하단'
            return form.stop_mode === 'instant'
              ? `보유 중 가격이 ${line}보다 한 번이라도 낮아지면 바로 보유 전량을 매도합니다(1차 매수 당일 포함${form.watch_mode === 'minute' ? ', 분 단위 감시는 완성된 봉의 종가' : ''}).`
              : `전일 종가가 ${line}보다 낮으면, 그날 ${m > 0 ? '손절선' : '하단'} 아래 가격에서 보유 전량을 매도합니다(1차 매수 다음 날부터, 장중 일시 이탈은 그날 무시).`
          })()}</p>
          {formError && editable && <div className="ct-error">{formError}</div>}
        </div>

        <div className="ct-plan">
          <div className="panel-title">매수 계획</div>
          <table className="data-table ct-legs">
            <thead>
              <tr><th>차수</th><th className="col-num">비율</th><th className="col-num">금액</th>
                <th>조건</th><th>상태</th></tr>
            </thead>
            <tbody>
              {item.legs.map((leg) => (
                <tr key={leg.no}>
                  <td>{leg.no}차</td>
                  <td className="col-num">{leg.ratio}%</td>
                  <td className="col-num">{fmt(leg.amount)}</td>
                  <td>{leg.no === 1
                    ? '구간 안 진입'
                    : leg.status === 'waiting' && bought && leg2?.trigger
                      ? <>구간 안 & ≤ {fmtPrice(leg2.trigger.price)}
                        <span className="muted"> ({leg2.trigger.basis === 'gap' ? `구간 상단 −폭 ${cfg.add_gap_pct}%` : `−${cfg.add_drop_pct}%`}, 1차가 −{leg2.dropPct.toFixed(1)}%)</span></>
                      : `1차 다음 날부터, ${addRuleText(cfg)}`}
                    {leg.no === 2 && leg2 && leg.status === 'waiting' && (
                      <div className={`ct-maxdrop ${leg2.possible ? 'muted' : 'down'}`}
                        title={leg2.afterFill
                          ? '1차 매수가에서 구간 하단까지 내려갈 수 있는 폭'
                          : `1차가 구간 상단에서 체결될 때 구간 안에서 내려갈 수 있는 최대 폭. 2차가 가능하려면 1차가 ${fmtPrice(leg2.minFirstPrice)} 이상에서 체결돼야 합니다.`}>
                        구간 안 최대 하락 {leg2.maxDropPct.toFixed(1)}%{leg2.afterFill ? '(1차가 기준)' : ''}
                        {leg2.possible ? '' : ' — 2차 불가'}
                      </div>
                    )}</td>
                  <td>{leg.status === 'filled'
                    ? <>{fmt(leg.qty)}주 @ {fmtPrice(leg.price)}{leg.confirmed ? '' : <span className="muted"> (잠정)</span>}
                      <span className="muted"> {leg.date}</span></>
                    : LEG_STATUS[leg.status]
                      ? <span className={leg.status === 'failed' ? 'down' : 'muted'}>{LEG_STATUS[leg.status]}</span>
                      : <span className="muted">대기</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="ct-actions">
            {editable && dirty && (
              <button disabled={busy || !!formError} onClick={save}>설정 저장</button>
            )}
            {draft && (
              <button className="primary" disabled={busy || !!formError || !zone || !!zoneFormError} onClick={start}>감시 시작</button>
            )}
            {item.phase === 'WATCHING' && (
              <button disabled={busy} onClick={() => run(() => actions.stopCandle(account.id, item.code), '감시 중지됨')}>감시 중지</button>
            )}
            {editable && (
              <button className={holding ? 'warn' : ''} disabled={busy}
                title="자동 매매를 멈추고 이 종목을 수동 매매 화면으로 옮깁니다(되돌릴 수 없음)"
                onClick={() => run(() => actions.handoffCandle(account.id, item.code), '수동 매매로 옮겼습니다')}>
                수동 전환
              </button>
            )}
            {!active && !draft && (
              <button disabled={busy}
                title={`이전 매매의 설정과 ${isZone ? '매입 구간' : '기준 캔들'}을 그대로 가져와 새로 '설정 중'으로 올립니다. 감시는 [감시 시작]을 눌러야 시작합니다.`}
                onClick={() => run(() => actions.redoCandle(account.id, item.code), '같은 설정으로 다시 올렸습니다 — 확인 후 [감시 시작]을 누르세요')}>
                다시 설정
              </button>
            )}
            {!active && (
              <button className="ghost" disabled={busy}
                onClick={() => run(() => actions.removeCandle(account.id, item.code))}>
                {draft ? '삭제' : '목록에서 지우기'}
              </button>
            )}
            {item.phase === 'MANUAL' && (
              <button className="ghost" onClick={() => navigate(ROUTES.MANUAL_TRADE)}>수동 매매로 이동</button>
            )}
          </div>
          {msg && <div className="ct-msg" role="status">{msg}</div>}
        </div>
      </div>

      {myLogs.length > 0 && (
        <ul className="ct-log">
          {myLogs.map((l, i) => (
            <li key={i}><span className="muted">{new Date(l.t).toLocaleTimeString('ko-KR', { hour12: false })}</span> {l.text.replace(`[${item.code}] ${label}: `, '')}</li>
          ))}
        </ul>
      )}
    </div>
  )
}
