// 종가 매매 페이지.
// [대금 양봉] 차트에서 [종가 매매 등록]한 종목이 '설정 중'으로 나타난다.
// 여러 거래일에 걸친 분할매수·청산이라 [매매] 목록(당일 상태머신)과 분리했다.
//   설정 중 ─[1차 매수]→ (장중) 즉시 시장가 → 다음 거래일부터 감시
//                     (장외) 다음 장 시가에 시장가 → 곧바로 감시
//   분할매수 감시: 직전 차수 체결가 −하락률 도달 시 다음 차수, 평단 +조기익절% 도달 시 전량 매도
//   분할 완료 후: 평단 +익절% / −손절% 전량 매도
//   [수동 전환]: 언제든 엔진을 멈추고 [매매] 목록(수동매매)으로 옮긴다.
import React, { useEffect, useMemo, useState } from 'react'
import { useStore } from '../store'
import Chart from '../components/Chart'
import PriceTicker from '../components/PriceTicker'
import { ROUTES, navigate } from '../router'

const fmt = (n) => Number(n || 0).toLocaleString('ko-KR')
const fmtPrice = (n) => (n ? Math.round(n).toLocaleString('ko-KR') : '—')

const PHASES = {
  DRAFT: { label: '설정 중', tone: 'idle' },
  PENDING_OPEN: { label: '시가 매수 예약', tone: 'wait' },
  WAIT_NEXT_DAY: { label: '다음 거래일 감시 대기', tone: 'wait' },
  ACCUMULATING: { label: '분할매수 감시', tone: 'live' },
  HOLDING: { label: '익절·손절 감시', tone: 'live' },
  DONE: { label: '종료', tone: 'end' },
  MANUAL: { label: '수동 전환됨', tone: 'end' },
}
const ACTIVE = ['PENDING_OPEN', 'WAIT_NEXT_DAY', 'ACCUMULATING', 'HOLDING']

export default function CloseTradePage() {
  const { accounts, closeTrades, phase } = useStore()
  const rows = useMemo(() => {
    const out = []
    accounts.forEach((a) => {
      Object.values(closeTrades[a.id] || {}).forEach((it) => out.push({ account: a, item: it }))
    })
    // 운용 중 → 설정 중 → 종료 순, 같은 그룹은 등록 최신순.
    const rank = (p) => (ACTIVE.includes(p) ? 0 : p === 'DRAFT' ? 1 : 2)
    return out.sort((x, y) => rank(x.item.phase) - rank(y.item.phase)
      || String(y.item.created_at).localeCompare(String(x.item.created_at)))
  }, [accounts, closeTrades])

  return (
    <main className="close-trade">
      <div className="close-trade-intro">
        <strong>종가 매매</strong>
        <span className="muted">
          [대금 양봉]에서 종목 차트를 열고 <b>종가 매매 등록</b>을 누르면 여기에 추가됩니다.
          설정을 확인한 뒤 <b>1차 매수</b>를 눌러야 주문이 나갑니다.
          {phase !== 'OPEN' && ' 지금은 장이 닫혀 있어 1차 매수는 다음 장 시가로 예약됩니다.'}
        </span>
        <button className="ghost" onClick={() => navigate(ROUTES.BIG_CANDLE)}>대금 양봉에서 고르기</button>
      </div>
      {rows.length === 0 && (
        <div className="screener-empty">등록된 종가 매매 종목이 없습니다.</div>
      )}
      <div className="panels-col">
        {rows.map(({ account, item }) => (
          <CloseTradeCard key={`${account.id}:${item.code}`} account={account} item={item} />
        ))}
      </div>
    </main>
  )
}

function toForm(cfg) {
  return {
    total_krw: cfg.total_krw,
    legs: cfg.ratios.length,
    ratios: [...cfg.ratios, 0, 0, 0].slice(0, 3),
    drops: [...cfg.add_drop_pcts, 5, 5].slice(0, 2),
    early_tp_pct: cfg.early_tp_pct,
    tp_pct: cfg.tp_pct,
    sl_pct: cfg.sl_pct,
  }
}

function fromForm(f) {
  const n = Number(f.legs)
  return {
    total_krw: Math.round(Number(f.total_krw) || 0),
    ratios: f.ratios.slice(0, n).map(Number),
    add_drop_pcts: f.drops.slice(0, n - 1).map(Number),
    early_tp_pct: Number(f.early_tp_pct),
    tp_pct: Number(f.tp_pct),
    sl_pct: Number(f.sl_pct),
  }
}

function CloseTradeCard({ account, item }) {
  const { ticks, closePositions, logs, actions, phase: marketPhase } = useStore()
  const [interval, setInterval] = useState(1440)
  const [form, setForm] = useState(() => toForm(item.config))
  const [dirty, setDirty] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const tick = (ticks[account.id] || {})[item.code]
  const pos = (closePositions[account.id] || {})[item.code]
  const ph = PHASES[item.phase] || { label: item.phase, tone: 'idle' }
  const draft = item.phase === 'DRAFT'
  const active = ACTIVE.includes(item.phase)
  const editable = draft || active
  const planLocked = !draft && item.phase !== 'PENDING_OPEN'   // 매수 시작 후엔 차수·비율 고정

  // 서버 설정이 바뀌면(다른 화면·저장 완료) 편집 중이 아닐 때만 폼을 맞춘다.
  useEffect(() => { if (!dirty) setForm(toForm(item.config)) }, [item.config, dirty])

  const cfg = fromForm(form)
  const ratioSum = cfg.ratios.reduce((a, b) => a + b, 0)
  const formError = cfg.total_krw < 10_000 ? '총 투자액은 1만원 이상이어야 합니다.'
    : Math.abs(ratioSum - 100) > 0.01 ? `분할 비율 합이 100%가 아닙니다(${ratioSum}%).`
      : cfg.ratios.some((r) => !(r > 0)) ? '분할 비율은 0보다 커야 합니다.'
        : cfg.add_drop_pcts.some((d) => !(d > 0 && d < 100)) ? '하락률은 0~100% 사이여야 합니다.'
          : [cfg.early_tp_pct, cfg.tp_pct, cfg.sl_pct].some((v) => !(v > 0 && v < 100)) ? '익절·손절률은 0~100% 사이여야 합니다.'
            : ''

  const set = (key, value) => { setForm((f) => ({ ...f, [key]: value })); setDirty(true) }
  const setAt = (key, i, value) => {
    setForm((f) => { const arr = [...f[key]]; arr[i] = value; return { ...f, [key]: arr } })
    setDirty(true)
  }

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
    const r = await run(() => actions.setCloseConfig(account.id, item.code, cfg), '설정 저장됨')
    if (r) setDirty(false)
    return r
  }

  const enter = async () => {
    if (dirty && !(await save())) return
    const open = marketPhase === 'OPEN'
    const first = Math.floor(cfg.total_krw * cfg.ratios[0] / 100)
    const message = `⚠️ ${account.label} 계좌 종가 매매 1차 매수\n${item.name} ${item.code}\n`
      + `${fmt(first)}원 시장가 ${open ? '— 지금 바로 주문이 나갑니다.' : '— 다음 장 시가에 주문이 나갑니다.'}\n진행할까요?`
    await run(() => actions.enterClose(account.id, item.code, message),
      open ? '1차 매수 주문 완료' : '다음 장 시가 매수 예약됨')
  }

  const myLogs = logs.filter((l) => l.account === account.id && l.text.startsWith(`[${item.code}] 종가매매`)).slice(-8)
  const price = tick?.price
  const pnlPct = item.shares > 0 && item.avg_cost > 0 && price ? (price - item.avg_cost) / item.avg_cost * 100 : null

  return (
    <div className={`stock-panel close-card tone-${ph.tone}`}>
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
      {item.exit_reason && !active && <div className="ct-exit">종료 사유: {item.exit_reason}</div>}

      <div className="ct-summary">
        <span>보유 <strong>{fmt(item.shares)}주</strong></span>
        <span>평단 <strong>{fmtPrice(item.avg_cost)}</strong></span>
        <span>평가 <strong className={pnlPct > 0 ? 'up' : pnlPct < 0 ? 'down' : ''}>
          {pnlPct === null ? '—' : `${pnlPct > 0 ? '+' : ''}${pnlPct.toFixed(2)}%`}</strong></span>
        {pos && <span className="muted">계좌 잔고 {fmt(pos.quantity)}주</span>}
        {item.phase === 'WAIT_NEXT_DAY' && <span className="muted">({item.quiet_date} 매수 — 다음 거래일부터 감시)</span>}
      </div>

      <div className="interval-selector ct-intervals">
        {[1440, 3].map((v) => (
          <button key={v} className={`iv-btn${interval === v ? ' active' : ''}`} aria-pressed={interval === v}
            onClick={() => setInterval(v)}>{v === 1440 ? '일봉' : '3분봉'}</button>
        ))}
      </div>
      <Chart account={account.id} code={item.code} interval={interval} tick={tick} height={260} />

      <div className="panels">
        <div className="ct-config">
          <div className="panel-title">매매 설정</div>
          <fieldset disabled={!editable || busy}>
            <label>총 투자액
              <input type="number" min="10000" step="10000" value={form.total_krw}
                onChange={(e) => set('total_krw', e.target.value)} />원
            </label>
            <label>분할 차수
              <select value={form.legs} disabled={planLocked} onChange={(e) => set('legs', Number(e.target.value))}>
                {[1, 2, 3].map((n) => <option key={n} value={n}>{n}차</option>)}
              </select>
            </label>
            <div className="ct-ratios">
              {Array.from({ length: form.legs }, (_, i) => (
                <label key={i}>{i + 1}차 비율
                  <input type="number" min="1" max="100" step="any" value={form.ratios[i]} disabled={planLocked}
                    onChange={(e) => setAt('ratios', i, e.target.value)} />%
                </label>
              ))}
            </div>
            {Array.from({ length: form.legs - 1 }, (_, i) => (
              <label key={i}>{i + 2}차 매수: {i + 1}차 체결가 대비
                <input type="number" min="0.1" max="99" step="any" value={form.drops[i]}
                  onChange={(e) => setAt('drops', i, e.target.value)} />% 하락
              </label>
            ))}
            {form.legs > 1 && (
              <label>분할 완료 전 익절: 평단 대비
                <input type="number" min="0.1" max="99" step="any" value={form.early_tp_pct}
                  onChange={(e) => set('early_tp_pct', e.target.value)} />% 상승
              </label>
            )}
            <label>{form.legs > 1 ? '분할 완료 후 ' : ''}익절: 평단 대비
              <input type="number" min="0.1" max="99" step="any" value={form.tp_pct}
                onChange={(e) => set('tp_pct', e.target.value)} />% 상승
            </label>
            <label>{form.legs > 1 ? '분할 완료 후 ' : ''}손절: 평단 대비
              <input type="number" min="0.1" max="99" step="any" value={form.sl_pct}
                onChange={(e) => set('sl_pct', e.target.value)} />% 하락
            </label>
          </fieldset>
          {formError && editable && <div className="ct-error">{formError}</div>}
        </div>

        <div className="ct-plan">
          <div className="panel-title">차수 계획</div>
          <table className="data-table ct-legs">
            <thead>
              <tr><th>차수</th><th className="col-num">비율</th><th className="col-num">금액</th>
                <th className="col-num">조건가</th><th>상태</th></tr>
            </thead>
            <tbody>
              {item.legs.map((leg) => (
                <tr key={leg.no}>
                  <td>{leg.no}차</td>
                  <td className="col-num">{leg.ratio}%</td>
                  <td className="col-num">{fmt(leg.amount)}</td>
                  <td className="col-num">{leg.no === 1 ? '시장가' : leg.trigger ? `≤ ${fmtPrice(leg.trigger)}` : '직전 차수 체결 후'}</td>
                  <td>{leg.status === 'filled'
                    ? <>{fmt(leg.qty)}주 @ {fmtPrice(leg.price)}{leg.confirmed ? '' : <span className="muted"> (잠정)</span>}</>
                    : leg.status === 'failed' ? <span className="down">실패·중단</span> : <span className="muted">대기</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="ct-actions">
            {editable && dirty && (
              <button disabled={busy || !!formError} onClick={save}>설정 저장</button>
            )}
            {draft && (
              <button className="primary" disabled={busy || !!formError} onClick={enter}>
                {marketPhase === 'OPEN' ? '1차 매수 (시장가)' : '1차 매수 예약 (다음 장 시가)'}
              </button>
            )}
            {item.phase === 'PENDING_OPEN' && (
              <button disabled={busy} onClick={() => run(() => actions.cancelClose(account.id, item.code), '예약 취소됨')}>예약 취소</button>
            )}
            {active && item.phase !== 'PENDING_OPEN' && (
              <button className="warn" disabled={busy}
                onClick={() => run(() => actions.handoffClose(account.id, item.code), '[매매] 목록으로 옮겼습니다')}>
                수동 전환
              </button>
            )}
            {!active && (
              <button className="ghost" disabled={busy}
                onClick={() => run(() => actions.removeClose(account.id, item.code))}>
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
            <li key={i}><span className="muted">{new Date(l.t).toLocaleTimeString('ko-KR', { hour12: false })}</span> {l.text.replace(`[${item.code}] 종가매매: `, '')}</li>
          ))}
        </ul>
      )}
    </div>
  )
}
