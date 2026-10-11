// 대금 양봉 매매 페이지.
// [대금 양봉] 차트에서 [대금 양봉 매매 등록]한 종목이 '설정 중'으로 나타난다(조회일 봉 = 기준 캔들).
// 여러 거래일에 걸친 매매라 [매매] 목록(당일 상태머신)과 분리했다.
//   매입 구간: 기준 캔들 몸통(시가~종가) 양 끝에서 몸통 길이의 x%씩 안쪽
//   설정 중 ─[감시 시작]→ 기준일 다음 거래일부터 가격이 구간에 들어오면 1차 매수(기한 N거래일)
//   2차: 1차 다음 날부터, 구간 안이면서 2차 조건가 이하(혼합: 하단까지 여유 > u%면 −u%·갭의 k% 중 깊은 쪽 / 고정 −u% / 갭의 k%)
//   평단 +p% → 절반 매도, 남은 매수 계획 취소, 나머지는 고점 −t% 트레일링 스톱
//   전일 종가 < 구간 하단 → 그날 하단 아래 가격에서 전량 매도
//   [수동 전환]: 보유 중 언제든 엔진을 멈추고 [매매] 목록(수동매매)으로 옮긴다.
import React, { useEffect, useMemo, useState } from 'react'
import { useStore } from '../store'
import Chart from '../components/Chart'
import PriceTicker from '../components/PriceTicker'
import { ROUTES, navigate } from '../router'
import { addRuleText, secondLegCheck, zoneOf } from '../candleZone'

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
const ACTIVE = ['WATCHING', 'ACCUMULATING', 'HOLDING', 'TRAILING']
const HOLDING = ['ACCUMULATING', 'HOLDING', 'TRAILING']
const WATCH_MINUTES = [1, 3, 5, 10, 15, 30, 60]
const LEG_STATUS = { failed: '실패·중단', canceled: '취소(익절)' }

export default function CandleTradePage() {
  const { accounts, candleTrades } = useStore()
  const rows = useMemo(() => {
    const out = []
    accounts.forEach((a) => {
      Object.values(candleTrades[a.id] || {}).forEach((it) => out.push({ account: a, item: it }))
    })
    // 운용 중 → 설정 중 → 종료 순, 같은 그룹은 등록 최신순.
    const rank = (p) => (ACTIVE.includes(p) ? 0 : p === 'DRAFT' ? 1 : 2)
    return out.sort((x, y) => rank(x.item.phase) - rank(y.item.phase)
      || String(y.item.created_at).localeCompare(String(x.item.created_at)))
  }, [accounts, candleTrades])

  return (
    <main className="candle-trade">
      <div className="candle-trade-intro">
        <strong>대금 양봉 매매</strong>
        <span className="muted">
          [대금 양봉]에서 종목 차트를 열고 <b>대금 양봉 매매 등록</b>을 누르면 조회일 봉을 기준 캔들로 여기에 추가됩니다.
          설정을 확인한 뒤 <b>감시 시작</b>을 누르면, 기준일 다음 거래일부터 가격이 매입 구간에 들어올 때 자동으로 매수합니다.
        </span>
        <button className="ghost" onClick={() => navigate(ROUTES.BIG_CANDLE)}>대금 양봉에서 고르기</button>
      </div>
      {rows.length === 0 && (
        <div className="screener-empty">등록된 대금 양봉 매매 종목이 없습니다.</div>
      )}
      <div className="panels-col">
        {rows.map(({ account, item }) => (
          <CandleTradeCard key={`${account.id}:${item.code}`} account={account} item={item} />
        ))}
      </div>
    </main>
  )
}

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
    trailing_pct: Number(f.trailing_pct),
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
  if (!(c.add_gap_pct > 0 && c.add_gap_pct <= 100)) return '2차 갭 비율은 0 초과 100% 이하여야 합니다.'
  if (!(c.add_min_drop_pct >= 0 && c.add_min_drop_pct < 100)) return '2차 최소 하락률은 0~100% 사이여야 합니다.'
  if (!(c.take_profit_pct > 0 && c.take_profit_pct <= 500)) return '절반 익절 상승률은 0~500% 사이여야 합니다.'
  if (!(c.trailing_pct > 0 && c.trailing_pct < 100)) return '트레일링 하락률은 0~100% 사이여야 합니다.'
  if (!(c.entry_days >= 1 && c.entry_days <= 60)) return '1차 매수 기한은 1~60거래일이어야 합니다.'
  return ''
}

function CandleTradeCard({ account, item }) {
  const { ticks, candlePositions, logs, actions } = useStore()
  const [interval, setInterval] = useState(1440)
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

  // 서버 설정이 바뀌면(다른 화면·저장 완료) 편집 중이 아닐 때만 폼을 맞춘다.
  useEffect(() => { if (!dirty) setForm(toForm(item.config)) }, [item.config, dirty])

  const cfg = fromForm(form)
  const formError = validate(cfg)
  // 저장된 기준 캔들과 (편집 중) 여백으로 구간을 미리 계산한다 — 저장 전에도 차트·표에 반영.
  const zone = (dirty && !bought ? zoneOf(item.base, cfg.zone_margin_pct) : item.zone) || item.zone
  // 2차 매수 가능성. 2차가 아직 대기 중이고 운용(또는 설정) 중일 때만 경고한다.
  const leg2Waiting = item.legs[1]?.status === 'waiting'
  const leg2 = secondLegCheck(zone, cfg, bought ? item.legs[0].price : 0)
  const leg2Warn = leg2 && !leg2.possible && leg2Waiting && editable
  const body = item.base.close - item.base.open

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
    const first = Math.floor(cfg.total_krw * cfg.first_ratio_pct / 100)
    const message = `⚠️ ${account.label} 계좌 대금 양봉 매매 감시 시작\n${item.name} ${item.code}\n`
      + `매입 구간 ${fmtPrice(zone?.low)} ~ ${fmtPrice(zone?.high)}에 들어오면 1차 ${fmt(first)}원 시장가 매수가 자동으로 나갑니다.\n진행할까요?`
    await run(() => actions.startCandle(account.id, item.code, message), '감시 시작됨')
  }

  const myLogs = logs.filter((l) => l.account === account.id && l.text.startsWith(`[${item.code}] 대금양봉`)).slice(-8)
  const price = tick?.price
  const pnlPct = item.shares > 0 && item.avg_cost > 0 && price ? (price - item.avg_cost) / item.avg_cost * 100 : null
  const inZone = zone && price ? price >= zone.low && price <= zone.high : null

  const lines = useMemo(() => {
    const out = []
    if (zone) {
      out.push({ price: zone.high, color: '#2e7d32', title: '구간 상단' })
      out.push({ price: zone.low, color: '#2e7d32', title: '구간 하단' })
    }
    if (item.legs[1]?.trigger && item.legs[1].possible) out.push({ price: item.legs[1].trigger, color: '#6a1b9a', title: '2차 ≤', dashed: true })
    if (item.avg_cost > 0 && holding) out.push({ price: item.avg_cost, color: '#555', title: '평단', dashed: true })
    if (item.take_profit_price) out.push({ price: item.take_profit_price, color: '#d32f2f', title: '절반 익절', dashed: true })
    if (item.trailing_stop) out.push({ price: item.trailing_stop, color: '#e65100', title: '트레일링', dashed: true })
    return out
  }, [zone?.low, zone?.high, item.legs, item.avg_cost, item.take_profit_price, item.trailing_stop, holding])

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
            : <>⚠️ 이 캔들은 2차 매수가 불가능합니다 — 구간 안 최대 하락률 {leg2.maxDropPct.toFixed(1)}%로는
              {' '}2차 조건({addRuleText(cfg)})을 만족할 수 없습니다. 1차만 매수됩니다.</>}
        </div>
      )}
      {item.exit_reason && !active && <div className="ct-exit">종료 사유: {item.exit_reason}</div>}

      <div className="ct-summary">
        <span title="조회일의 기준 캔들">기준 캔들 <strong>{item.base.date}</strong>{' '}
          {fmtPrice(item.base.open)} → {fmtPrice(item.base.close)}
          {!item.base.confirmed && <span className="muted" title="다음 거래일 감시 시작 때 일봉 확정값으로 다시 맞춥니다"> (등록값)</span>}
        </span>
        <span>매입 구간 <strong className={inZone ? 'up' : ''}>{zone ? `${fmtPrice(zone.low)} ~ ${fmtPrice(zone.high)}` : '—'}</strong>
          {inZone && <span className="muted"> · 현재가 구간 안</span>}</span>
        {active && item.day && (
          <span className="muted">
            {item.days_elapsed > 0 ? `기준일 이후 ${item.days_elapsed}거래일째` : '기준일 당일(감시는 다음 거래일부터)'}
            {item.phase === 'WATCHING' && item.days_elapsed > 0 && ` / 기한 ${item.config.entry_days}거래일`}
          </span>
        )}
        {item.stop_armed && <span className="down">전일 종가 {fmtPrice(item.prev_close)} &lt; 하단 — 오늘 하단 아래에서 전량 매도</span>}
      </div>
      {(bought || item.shares > 0) && (
        <div className="ct-summary">
          <span>보유 <strong>{fmt(item.shares)}주</strong></span>
          <span>평단 <strong>{fmtPrice(item.avg_cost)}</strong></span>
          <span>평가 <strong className={pnlPct > 0 ? 'up' : pnlPct < 0 ? 'down' : ''}>
            {pnlPct === null ? '—' : `${pnlPct > 0 ? '+' : ''}${pnlPct.toFixed(2)}%`}</strong></span>
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
      <Chart account={account.id} code={item.code} interval={interval} tick={tick} height={260}
        anchorDate={item.base.date} priceLines={lines} />

      <div className="panels">
        <div className="ct-config">
          <div className="panel-title">매매 설정</div>
          <fieldset disabled={!editable || busy}>
            <label>총 투자액
              <input type="number" min="10000" step="10000" value={form.total_krw}
                onChange={(e) => set('total_krw', e.target.value)} />원
            </label>
            <label title="1차 매수에 쓰는 비중. 나머지가 2차 매수 금액입니다.">1차 매수 비중
              <input type="number" min="10" max="90" step="any" value={form.first_ratio_pct} disabled={bought}
                onChange={(e) => set('first_ratio_pct', e.target.value)} />%
              <span className="muted">(2차 {Math.max(0, 100 - Number(form.first_ratio_pct || 0))}%)</span>
            </label>
            <label title={`몸통 ${fmtPrice(body)}원의 이 비율만큼 시가 위·종가 아래를 구간 끝으로 잡습니다.`}>매입 구간: 몸통 양 끝에서
              <input type="number" min="0" max="49" step="any" value={form.zone_margin_pct} disabled={bought}
                onChange={(e) => set('zone_margin_pct', e.target.value)} />% 안쪽
            </label>
            <label title="기준일 다음 거래일부터 셉니다. 이 안에 구간 진입이 없으면 매매하지 않고 종료합니다.">1차 매수 기한: 기준일 이후
              <input type="number" min="1" max="60" step="1" value={form.entry_days} disabled={bought}
                onChange={(e) => set('entry_days', e.target.value)} />거래일
            </label>
            <label title="1차 매수 다음 날부터, 가격이 매입 구간 안이면서 2차 조건가 이하일 때 2차를 삽니다. 혼합: 1차가에서 구간 하단까지 여유가 문턱을 넘을 때만, 문턱 하락률과 갭 비율 중 더 깊이 내려간 가격에서 삽니다(넓은 구간에서 너무 위쪽 매수를 피하고, 최소 문턱만큼은 내려감).">2차 매수 기준
              <select value={form.add_mode} onChange={(e) => set('add_mode', e.target.value)}>
                <option value="mixed">혼합</option>
                <option value="fixed">고정 하락률</option>
                <option value="gap">갭 비율</option>
              </select>
            </label>
            {form.add_mode === 'fixed' && (
              <label className="ct-sub" title="2차 조건가 = 1차 매수가 × (1 − 이 비율)">2차: 1차 매수가 대비
                <input type="number" min="0.1" max="99" step="any" value={form.add_drop_pct}
                  onChange={(e) => set('add_drop_pct', e.target.value)} />% 하락
              </label>
            )}
            {form.add_mode === 'mixed' && (
              <label className="ct-sub" title="1차 매수가에서 구간 하단까지 내려갈 여유가 이 비율 이하이면 2차를 하지 않습니다.">
                1차가~구간 하단 여유가
                <input type="number" min="0.1" max="99" step="any" value={form.add_drop_pct}
                  onChange={(e) => set('add_drop_pct', e.target.value)} />% 초과일 때만 2차(최소 이만큼 하락)
              </label>
            )}
            {form.add_mode !== 'fixed' && (
              <label className="ct-sub" title="2차 조건가 = 1차 매수가 − (1차 매수가 − 구간 하단) × 이 비율">
                {form.add_mode === 'mixed' ? '또는 ' : '2차: '}1차가~구간 하단 갭의
                <input type="number" min="1" max="100" step="any" value={form.add_gap_pct}
                  onChange={(e) => set('add_gap_pct', e.target.value)} />% 하락한 가격{form.add_mode === 'mixed' ? ' 중 더 깊은 곳' : ''}
              </label>
            )}
            {form.add_mode === 'gap' && (
              <>
                <label className="ct-sub" title="1차가 하단에 붙어 체결돼 갭이 작으면, 같은 가격에 금액만 두 배로 사지 않도록 2차를 하지 않습니다.">
                  갭 기준 최소 하락률
                  <input type="number" min="0" max="99" step="any" value={form.add_min_drop_pct}
                    onChange={(e) => set('add_min_drop_pct', e.target.value)} />% (이보다 얕으면 2차 없음)
                </label>
              </>
            )}
            <label title="도달하면 보유의 절반을 팔고, 아직 사지 않은 2차 계획은 취소합니다.">절반 익절: 평단 대비
              <input type="number" min="0.1" max="500" step="any" value={form.take_profit_pct}
                onChange={(e) => set('take_profit_pct', e.target.value)} />% 상승
            </label>
            <label title="절반 익절 뒤 나머지는 고점에서 이만큼 내려오면 전량 매도합니다.">트레일링 스톱: 고점 대비
              <input type="number" min="0.1" max="99" step="any" value={form.trailing_pct}
                onChange={(e) => set('trailing_pct', e.target.value)} />% 하락
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
          <p className="muted ct-rule">전일 종가가 매입 구간 하단 아래면, 그날 하단 아래 가격에서 보유 전량을 매도합니다(1차 매수 다음 날부터).</p>
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
                        <span className="muted"> ({leg2.trigger.basis === 'gap' ? `갭 ${cfg.add_gap_pct}%` : `−${cfg.add_drop_pct}%`}, 1차가 −{leg2.dropPct.toFixed(1)}%)</span></>
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
              <button className="primary" disabled={busy || !!formError || !zone} onClick={start}>감시 시작</button>
            )}
            {item.phase === 'WATCHING' && (
              <button disabled={busy} onClick={() => run(() => actions.stopCandle(account.id, item.code), '감시 중지됨')}>감시 중지</button>
            )}
            {holding && (
              <button className="warn" disabled={busy}
                onClick={() => run(() => actions.handoffCandle(account.id, item.code), '[매매] 목록으로 옮겼습니다')}>
                수동 전환
              </button>
            )}
            {!active && (
              <button className="ghost" disabled={busy}
                onClick={() => run(() => actions.removeCandle(account.id, item.code))}>
                {draft ? '삭제' : '목록에서 지우기'}
              </button>
            )}
            {item.phase === 'MANUAL' && (
              <button className="ghost" onClick={() => navigate(ROUTES.TRADING)}>[매매]로 이동</button>
            )}
          </div>
          {msg && <div className="ct-msg" role="status">{msg}</div>}
        </div>
      </div>

      {myLogs.length > 0 && (
        <ul className="ct-log">
          {myLogs.map((l, i) => (
            <li key={i}><span className="muted">{new Date(l.t).toLocaleTimeString('ko-KR', { hour12: false })}</span> {l.text.replace(`[${item.code}] 대금양봉: `, '')}</li>
          ))}
        </ul>
      )}
    </div>
  )
}
