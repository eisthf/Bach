// 무상증자 매매 페이지.
// 서버가 KIND 공시를 감시하다가 무상증자 결정 공시가 나오면, 자동 매수를 켠 계좌에서 그 종목을
// 수동 매매 목록(origin "bonus")에 카드로 올리고 매수 작업을 돌린다(backend/app/bonus_issue.py).
// 사지 않는 공시(유무상 제외·이미 재개·시가 상한 초과 등)도 '수동' 카드로 올린다.
//   계좌별 설정: 자동 매수 켜기(기본 꺼짐) · 금액(기본 100만 원) · 주문 방식(기본 예상 체결가) ·
//                익절 +20%(전량 / 절반 후 트레일링) · 손절 −15% · 장전 공시 시가 상한 +10% · 재개가 건너뛰기 · 유무상 포함
//   종목 카드: 상태(매수 대기 / 자동 감시 / 수동 / 갭상승 대기), 권리락 일정, 일봉(공시·권리락 표시) + 1시간봉.
//     [수동으로]는 언제든 — 수동에서 [갭상승 모드]를 켤 수 있다. 화면을 떠나는 건 [수동 매매 화면으로]·
//     [구간 매매로](보유 0)·✕(보유 0) 세 가지뿐이다. 손절·익절은 평단 대비 % 또는 차트에 그은 선으로 바꾼다.
import React, { useEffect, useMemo, useState } from 'react'
import { useStore } from '../store'
import Chart from '../components/Chart'
import PriceTicker from '../components/PriceTicker'
import ManualTradePanel from '../components/ManualTradePanel'
import ManualExits, { ruleBody, useExitDraft, useExitLines } from '../components/ManualExits'
import { roundToTick } from '../candleZone'
import { useHiddenCards } from '../components/TradeChipBar'
import AccountViewFilter, { useAccountView } from '../components/AccountViewFilter'
import { ManualChipBar } from './ManualTradePage'

const fmt = (n) => Math.round(Number(n || 0)).toLocaleString('ko-KR')
const hhmm = (iso) => (iso ? iso.slice(11, 16) : '')
const mmdd = (iso) => (iso ? iso.slice(5, 10) : '')

export const ORDER_MODES = [
  { value: 'expected', label: '예상 체결가 (기본)',
    help: '첫 체결(단일가 마감) 예상 시각 직전에 예상체결가로 수량을 정해 상한가 지정가로 냅니다. 100만 원에 가깝게 사지만 마감 직전 예상가가 오르면 조금 넘을 수 있습니다. 예상가가 없거나 먼저 체결이 나면 첫 체결가 방식으로 넘어갑니다.' },
  { value: 'upper_limit', label: '상한가 지정가',
    help: '감지 즉시 상한가 지정가, 수량 = 금액 ÷ 상한가. 금액을 절대 넘지 않지만 덜 삽니다. 매매정지 중 거부되면 받아질 때까지 재시도합니다.' },
  { value: 'market', label: '시장가',
    help: '감지 즉시 시장가, 수량 = 금액 ÷ 공시 전 현재가. 재개가가 +13~30% 뛰면 실제 금액이 그만큼 넘습니다. 거부되면 재시도합니다.' },
  { value: 'after_resume', label: '재개 후 첫 체결가',
    help: '매매가 재개돼 첫 체결이 나면 그 가격으로 수량을 정해 시장가로 삽니다. 금액은 거의 정확하지만 재개 단일가에는 못 들어가고, 재개가가 상한가면 체결이 안 될 수 있습니다.' },
]
const MODE_LABEL = Object.fromEntries(ORDER_MODES.map((m) => [m.value, m.label.replace(' (기본)', '')]))
const JOB_STATUS = {
  waiting: '매수 대기', ordering: '주문 중', sent: '주문 전송', skipped: '건너뜀',
  failed: '실패', cancelled: '취소',
}
const EX_COLOR = '#7b1fa2'
const DISCLOSE_COLOR = '#ef6c00'
const todayIso = () => {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
const dayDiff = (a, b) => Math.round((Date.parse(`${a}T00:00:00Z`) - Date.parse(`${b}T00:00:00Z`)) / 86400000)

// 카드 상태. 매수 대기 > 갭상승 대기 > 자동 감시(손절·익절 켜짐) > 수동.
function cardMode(item, job) {
  if (job && ['waiting', 'ordering'].includes(job.status)) return { key: 'waiting', label: '매수 대기' }
  if (job?.gap) return { key: 'gap', label: '갭상승 대기' }
  if (item.exits?.stop?.enabled || item.exits?.take_profit?.enabled) return { key: 'auto', label: '자동 감시' }
  return { key: 'manual', label: '수동' }
}

export default function BonusTradePage() {
  const { accounts, manualTrades, bonus } = useStore()
  const rows = useMemo(() => {
    const out = []
    accounts.forEach((a) => {
      Object.values(manualTrades[a.id] || {}).filter((it) => it.origin === 'bonus')
        .forEach((it) => out.push({ account: a, item: it }))
    })
    return out.sort((x, y) => String(y.item.created_at).localeCompare(String(x.item.created_at)))
  }, [accounts, manualTrades])
  const { isHidden, toggle, solo } = useHiddenCards('bach.hidden.bonusTrade')
  const { view, choose, visible } = useAccountView('bach.accountView.bonusTrade', accounts)
  const inView = rows.filter(({ account }) => visible(account))
  const shown = inView.filter(({ account, item }) => !isHidden(account, item))
  const demo = accounts.length > 0 && accounts.every((a) => !a.live)

  return (
    <main className="candle-trade bonus-trade">
      <div className="candle-trade-intro">
        <strong>무상증자 매매</strong>
        <span className="muted">
          KIND 공시를 감시하다가 <b>무상증자 결정</b> 공시가 나오면, 자동 매수를 켠 계좌에서 그 종목을 삽니다.
          장중 공시는 매매정지 후 약 40분 뒤 재개 단일가에서 사고, 장전·장후 공시는 시가가 기준가 대비 상한(기본 +10%)
          이하일 때만 삽니다. 사지 않은 공시도 '수동' 카드로 남습니다 — 카드는 권리락 뒤 지켜보다 사람이 지웁니다.
        </span>
      </div>
      <WatchStatus demo={demo} />
      <div className="bonus-settings-row">
        {accounts.filter(visible).map((a) => (
          <BonusSettingsForm key={a.id} account={a} state={bonus[a.id]} />
        ))}
      </div>
      <JobList accounts={accounts.filter(visible)} bonus={bonus} />
      <AccountViewFilter accounts={accounts} rows={rows} view={view} choose={choose} />
      <ManualChipBar rows={inView} isHidden={isHidden} toggle={toggle} solo={solo}
        showAccount={accounts.length > 1} tag="무상" />
      {rows.length > 0 && inView.length === 0 && (
        <div className="screener-empty">이 계좌에는 종목이 없습니다.</div>
      )}
      {inView.length > 0 && shown.length === 0 && (
        <div className="screener-empty">모든 종목이 숨김 상태입니다. 위 종목 배지를 누르면 다시 보입니다.</div>
      )}
      <div className="panels-col">
        {shown.map(({ account, item }) => (
          <BonusTradeCard key={`${account.id}:${item.code}`} account={account} item={item}
            job={(bonus[account.id]?.jobs || []).find((j) => j.code === item.code)} />
        ))}
      </div>
    </main>
  )
}

// 공시 감시 상태 + 최근 공시. 데모(키움 계좌 없음)에서는 가짜 공시를 넣어 볼 수 있다.
function WatchStatus({ demo }) {
  const { bonusWatch, actions } = useStore()
  // 서버는 새 공시·상태 변화 때만 방송한다(조회 10초마다가 아니라). '마지막 확인'은 이 화면을 보는
  // 동안만 30초마다 REST로 다시 읽는다 — 로컬 서버 호출이라 키움 게이트와 무관하다.
  useEffect(() => {
    actions.refreshBonusWatch()
    const id = window.setInterval(actions.refreshBonusWatch, 30000)
    return () => window.clearInterval(id)
  }, [actions])
  const st = bonusWatch?.status
  const events = bonusWatch?.events || []
  const [open, setOpen] = useState(false)
  return (
    <section className="bonus-watch">
      <div className="bonus-watch-head">
        <span className={`bonus-dot ${!st?.enabled ? 'off' : st?.ok === false ? 'err' : st?.ok ? 'ok' : ''}`} />
        <b>공시 감시</b>
        <span className="muted">
          {!st ? '불러오는 중' : !st.enabled ? '꺼짐(BONUS_WATCH=0)'
            : `${st.source} · 평일 07~20시 ${st.poll_sec >= 60 ? `${st.poll_sec / 60}분` : `${st.poll_sec}초`}마다(08:59:30~09:01:30 쉼) · 마지막 확인 ${st.last_poll ? hhmm(st.last_poll) + st.last_poll.slice(16, 19) : '—'}`}
        </span>
        {st?.error && <span className="input-err">{st.error}</span>}
        <button className="link-btn" onClick={() => setOpen((v) => !v)}>
          최근 공시 {events.length}건 {open ? '접기' : '보기'}
        </button>
      </div>
      {open && (
        events.length === 0
          ? <div className="muted bonus-empty">감지한 무상증자 공시가 없습니다.</div>
          : (
            <table className="bonus-events">
              <thead><tr><th>공시</th><th>종목</th><th>제목</th><th>처리</th></tr></thead>
              <tbody>
                {events.map((e) => (
                  <tr key={e.acptno}>
                    <td>{mmdd(e.disclosed_at)} {hhmm(e.disclosed_at)}</td>
                    <td>{e.corp} <span className="muted">{e.code}</span></td>
                    <td>{e.title}</td>
                    <td>{!e.eligible ? `대상 아님(${e.why})` : e.paid_too ? '유무상' : '계좌별 처리(아래 매수 작업)'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
      )}
      {demo && <SimulateForm />}
    </section>
  )
}

function SimulateForm() {
  const { actions } = useStore()
  const [code, setCode] = useState('')
  const [paid, setPaid] = useState(false)
  const [msg, setMsg] = useState('')
  const submit = async (e) => {
    e.preventDefault()
    try {
      await actions.simulateBonus({ code: code.trim(), paid_too: paid })
      setMsg('가짜 공시를 넣었습니다')
      setCode('')
    } catch (e2) {
      setMsg(String(e2.message || e2))
    }
  }
  return (
    <form className="zone-add bonus-sim" onSubmit={submit}>
      <span className="muted">데모: 가짜 공시</span>
      <input placeholder="종목코드" value={code} onChange={(e) => setCode(e.target.value)} aria-label="가짜 공시 종목코드" />
      <label className="checkfield"><input type="checkbox" checked={paid} onChange={(e) => setPaid(e.target.checked)} />유무상</label>
      <button type="submit" className="add-btn" disabled={!code.trim()}>공시 발생</button>
      {msg && <span className="muted">{msg}</span>}
    </form>
  )
}

function BonusSettingsForm({ account, state }) {
  const { actions } = useStore()
  const server = state?.settings
  const [draft, setDraft] = useState(server || null)
  const [dirty, setDirty] = useState(false)
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const serverKey = JSON.stringify(server || {})
  useEffect(() => {
    if (!dirty && server) setDraft(server)
  }, [serverKey]) // eslint-disable-line react-hooks/exhaustive-deps
  if (!draft) return null
  const set = (patch) => { setDraft((d) => ({ ...d, ...patch })); setDirty(true); setMsg('') }
  const mode = ORDER_MODES.find((m) => m.value === draft.order_mode)

  const save = async (patch = {}) => {
    setBusy(true)
    try {
      const r = await actions.setBonusSettings(account.id, { ...draft, ...patch })
      if (r) {
        setDirty(false)
        setMsg('저장됨')
      }
    } catch (e) {
      setMsg(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }
  // 스위치는 바로 저장한다(켜고 [저장]을 잊으면 공시를 놓친다). 나머지 값도 함께 저장된다.
  const toggleEnabled = (on) => { setDraft((d) => ({ ...d, enabled: on })); save({ enabled: on }) }

  return (
    <section className={`bonus-settings ${account.danger ? 'danger' : 'safe'}`}>
      <div className="bonus-settings-head">
        <span className={`account-badge ${account.danger ? 'danger' : 'safe'}`}>
          {account.danger ? '⚠️ 실전' : account.label}
        </span>
        <label className={`bonus-switch ${draft.enabled ? 'on' : ''}`}>
          <input type="checkbox" checked={!!draft.enabled} disabled={busy}
            onChange={(e) => toggleEnabled(e.target.checked)} />
          자동 매수 {draft.enabled ? '켜짐' : '꺼짐'}
        </label>
      </div>
      <div className="bonus-fields">
        <label>금액(원)
          <input type="number" min="10000" step="10000" value={draft.budget}
            onChange={(e) => set({ budget: e.target.value })} />
        </label>
        <label>주문 방식
          <select value={draft.order_mode} onChange={(e) => set({ order_mode: e.target.value })}>
            {ORDER_MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
          </select>
        </label>
        <label>익절 평단 +%
          <input type="number" min="0.1" step="0.5" value={draft.take_profit_pct}
            onChange={(e) => set({ take_profit_pct: e.target.value })} />
        </label>
        <label className="checkfield" title="익절 기준에서 절반을 팔고, 나머지는 고점 대비 −t% 또는 익절 기준가 되밀림에 판다">
          <input type="checkbox" checked={!!draft.take_profit_partial}
            onChange={(e) => set({ take_profit_partial: e.target.checked })} />
          절반 익절 후 트레일링
        </label>
        {draft.take_profit_partial && (
          <label>트레일링 고점 −%
            <input type="number" min="0.5" step="0.5" value={draft.trail_pct}
              onChange={(e) => set({ trail_pct: e.target.value })} />
          </label>
        )}
        <label>손절 평단 −%
          <input type="number" min="0.1" step="0.5" value={draft.stop_pct}
            onChange={(e) => set({ stop_pct: e.target.value })} />
        </label>
        <label title="장전·장후 공시(그리고 재개가 15:20을 넘는 장중 공시)는 다음 장 시작 단일가에서 삽니다. 시가 예상가가 기준가 대비 이 %를 넘으면 사지 않습니다. 0이면 제한 없음.">
          장전 공시 시가 상한 +%
          <input type="number" min="0" max="30" step="1" value={draft.open_max_pct}
            onChange={(e) => set({ open_max_pct: e.target.value })} />
        </label>
        <label title="장중 공시: 재개 예상체결가·첫 체결가가 기준가(전일 종가) 대비 이 % 이상이면 사지 않습니다. 0이면 끔.">
          장중 재개가 건너뛰기 +%
          <input type="number" min="0" max="30" step="1" value={draft.skip_above_pct}
            onChange={(e) => set({ skip_above_pct: e.target.value })} />
        </label>
        <label className="checkfield">
          <input type="checkbox" checked={!!draft.include_paid} onChange={(e) => set({ include_paid: e.target.checked })} />
          유무상증자도 매수
        </label>
        <details className="bonus-adv">
          <summary>시각</summary>
          <label>장중 공시 → 첫 체결(분)
            <input type="number" min="1" max="120" value={draft.resume_min}
              onChange={(e) => set({ resume_min: e.target.value })} />
          </label>
          <label>예상가 주문: 마감 몇 초 전
            <input type="number" min="3" max="120" value={draft.lead_sec}
              onChange={(e) => set({ lead_sec: e.target.value })} />
          </label>
        </details>
      </div>
      {mode && <div className="bonus-mode-help">{mode.help}</div>}
      <div className="exit-actions">
        <button className="save-btn" disabled={busy || !dirty} onClick={() => save()}>저장</button>
        {dirty && <button className="ghost" disabled={busy} onClick={() => { setDraft(server); setDirty(false) }}>되돌리기</button>}
        {dirty && <span className="exit-dirty">저장 안 됨</span>}
        {msg && <span className="trade-msg">{msg}</span>}
      </div>
      <div className="manual-stop-note">
        손절·익절 기본값은 새로 산 종목에만 적용됩니다. 산 뒤에는 종목 카드에서 바꾸세요.
      </div>
    </section>
  )
}

// 매수 작업(대기·결과) 목록. 카드에 없는 것(건너뜀 등)도 여기서 보인다.
function JobList({ accounts, bonus }) {
  const { actions } = useStore()
  const jobs = accounts.flatMap((a) => (bonus[a.id]?.jobs || []).map((j) => ({ account: a, job: j })))
  if (jobs.length === 0) return null
  const cancel = async (a, code) => {
    if (!window.confirm(`${code} 매수 대기를 취소할까요?`)) return
    try { await actions.cancelBonusJob(a.id, code) } catch (e) { window.alert(String(e.message || e)) }
  }
  return (
    <table className="bonus-events bonus-jobs">
      <thead><tr><th>계좌</th><th>종목</th><th>공시</th><th>방식</th><th>상태</th><th /></tr></thead>
      <tbody>
        {jobs.slice(0, 20).map(({ account, job }) => (
          <tr key={`${account.id}:${job.code}:${job.acptno}`} className={`job-${job.status}`}>
            <td>{account.label}</td>
            <td>{job.name} <span className="muted">{job.code}</span></td>
            <td>{mmdd(job.disclosed_at)} {hhmm(job.disclosed_at)}</td>
            <td>{MODE_LABEL[job.mode] || job.mode}</td>
            <td>
              <b>{JOB_STATUS[job.status] || job.status}</b>
              {job.status === 'waiting' && ` · 첫 체결 예상 ${job.target_at.slice(0, 10) !== job.disclosed_at.slice(0, 10) ? mmdd(job.target_at) + ' ' : ''}${hhmm(job.target_at)}`}
              {job.message && <span className="muted"> · {job.message}</span>}
            </td>
            <td>
              {['waiting', 'ordering'].includes(job.status) && (
                <button className="link-btn" onClick={() => cancel(account, job.code)}>취소</button>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// 권리락 일정 한 줄 + 일봉 표시. 권리락일은 신주배정기준일 직전 평일(예정) → KIND 권리락 안내로 확정.
function exSchedule(job) {
  if (!job) return { text: '', marks: [] }
  const parts = []
  if (job.ratio > 0) parts.push(`1주당 ${job.ratio}주`)
  if (job.record_date) parts.push(`신주배정기준일 ${mmdd(job.record_date)}`)
  if (job.ex_date) {
    const d = dayDiff(job.ex_date, todayIso())
    const when = d > 0 ? `D-${d}` : d === 0 ? '오늘' : `${-d}일 지남`
    parts.push(`권리락일 ${job.ex_date} (${job.ex_confirmed ? '확정' : '예정'}, ${when})`)
  }
  if (job.ex_notice_at) parts.push(`권리락 안내 ${mmdd(job.ex_notice_at)} ${hhmm(job.ex_notice_at)}`)
  if (job.listing_date) parts.push(`상장예정 ${mmdd(job.listing_date)}`)
  const marks = [{ at: job.disclosed_at.slice(0, 16), text: `공시 ${hhmm(job.disclosed_at)}`, color: DISCLOSE_COLOR }]
  if (job.ex_notice_at) marks.push({ at: job.ex_notice_at.slice(0, 16), text: '권리락 안내', color: EX_COLOR, shape: 'square' })
  if (job.ex_date) marks.push({ at: job.ex_date, text: job.ex_confirmed ? '권리락일' : '권리락일(예정)', color: EX_COLOR, position: 'belowBar', shape: 'arrowUp' })
  return { text: parts.length ? parts.join(' · ') : '권리락 일정 아직 모름 — DART 조건·KIND 권리락 안내를 기다리는 중', marks }
}

function BonusTradeCard({ account, item, job }) {
  const { ticks, actions } = useStore()
  const tick = (ticks[account.id] || {})[item.code]
  const verified = item.position_verified !== false
  const exitDraft = useExitDraft(item)
  const avg = verified ? Number(item.position?.avg_price || 0) : 0
  const { lines, drag } = useExitLines(exitDraft.draft, avg, item.trailing)
  const mode = cardMode(item, job)
  const sched = exSchedule(job)
  const [gapOpen, setGapOpen] = useState(false)
  const run = async (fn, ask) => {
    if (ask && !window.confirm(ask)) return
    try { await fn() } catch (e) { window.alert(String(e.message || e)) }
  }
  const onLineDrag = (side, p) => {
    exitDraft.update(side, { mode: 'price', price: roundToTick(p), enabled: true })
    if (exitDraft.pick === side) exitDraft.setPick(null)
  }
  const remove = async () => {
    if (!window.confirm(`${item.code} 를 목록에서 지울까요? (계좌 보유가 0일 때만 지워집니다)`)) return
    try {
      await actions.removeManual(account.id, item.code)
    } catch (e) {
      window.alert(String(e.message || e))
    }
  }
  const chartProps = {
    account: account.id, code: item.code, tick, height: 300, priceLines: lines,
    draggableLines: drag, onLineDrag, onPriceClick: exitDraft.pick ? exitDraft.drawAt : null,
    eventMarks: sched.marks,
  }

  return (
    <div className={`stock-panel manual-card bonus-card ${account.danger ? 'danger' : 'safe'}`}>
      <div className="panel-header">
        <div className="panel-id">
          <span className="panel-code">{item.code}</span>
          <span className="panel-name">{item.name}</span>
          <span className={`account-badge ${account.danger ? 'danger' : 'safe'}`}>
            {account.danger ? '⚠️ 실전' : account.label}
          </span>
          <span className={`origin-badge bonus-mode-${mode.key}`}>{mode.label}</span>
        </div>
        <PriceTicker tick={tick} />
        <div className="panel-actions">
          <button className="remove-btn" onClick={remove} title="목록에서 지우기(보유 0일 때)">✕</button>
        </div>
      </div>
      <div className="bonus-card-info">
        {job && <span>📰 {mmdd(job.disclosed_at)} {hhmm(job.disclosed_at)} {job.title}</span>}
        {job && (
          <span className="muted">
            {MODE_LABEL[job.mode] || job.mode}
            {job.status === 'waiting' && ` · 첫 체결 예상 ${hhmm(job.target_at)}`}
            {` · ${JOB_STATUS[job.status] || job.status}`}
            {job.qty > 0 && ` · ${fmt(job.qty)}주 (수량 기준 ${job.basis} ${fmt(job.price)}원)`}
            {job.message && ` · ${job.message}`}
          </span>
        )}
        {job && <span className="bonus-ex">📅 {sched.text}</span>}
      </div>
      <div className="bonus-card-actions">
        {mode.key !== 'manual' && (
          <button className="ghost" onClick={() => run(() => actions.bonusToManual(account.id, item.code),
            `${item.name}: ${mode.key === 'waiting' ? '매수 대기를 취소하고 ' : mode.key === 'gap' ? '갭상승 모드를 끄고 ' : '자동 손절·익절을 끄고 '}수동으로 바꿀까요?`)}>
            수동으로
          </button>
        )}
        {mode.key === 'manual' && (
          <button className={gapOpen ? 'ghost on' : 'ghost'} onClick={() => setGapOpen((v) => !v)}>
            갭상승 모드{gapOpen ? ' 닫기' : ' 켜기'}
          </button>
        )}
        <span className="bonus-actions-gap" />
        <button className="link-btn" onClick={() => run(() => actions.bonusToManualPage(account.id, item.code),
          `${item.name}을(를) 수동 매매 화면으로 옮길까요? (무상증자 화면에서 빠집니다. 손절·익절 설정은 그대로)`)}>
          수동 매매 화면으로
        </button>
        <button className="link-btn" onClick={() => run(() => actions.bonusToZone(account.id, item.code),
          `${item.name}을(를) 구간 매매(설정 중)로 옮길까요? 보유가 0일 때만 됩니다.`)}>
          구간 매매로
        </button>
      </div>
      {job?.gap && <GapStatus account={account} item={item} gap={job.gap} />}
      {mode.key === 'manual' && gapOpen && (
        <GapForm account={account} item={item} tick={tick} onDone={() => setGapOpen(false)} />
      )}
      {item.notice && (
        <div className="recovery-notice" role="alert">
          {item.notice}
          <button className="link-btn notice-ok" onClick={() => actions.clearManualNotice(account.id, item.code)}>확인</button>
        </div>
      )}
      {!verified && <div className="recovery-notice" role="alert">잔고 조회 실패 — 표시 수량을 신뢰하지 말고 계좌에서 확인하세요.</div>}
      {exitDraft.pick && (
        <div className="chart-toolbar">
          <span className={`draw-hint draw-${exitDraft.pick}`}>
            아무 차트나 눌러 {exitDraft.pick === 'stop' ? '손절' : '익절'} 라인을 그으세요
            <button type="button" className="link-btn" onClick={() => exitDraft.setPick(null)}>취소</button>
          </span>
        </div>
      )}
      <div className="bonus-charts">
        <div>
          <div className="bonus-chart-label">일봉</div>
          <Chart {...chartProps} prefKey={`bonus:${account.id}:${item.code}:day`} interval={1440} />
        </div>
        <div>
          <div className="bonus-chart-label">1시간봉</div>
          <Chart {...chartProps} prefKey={`bonus:${account.id}:${item.code}:hour`} interval={60} />
        </div>
      </div>
      <div className="panels manual-panels">
        <ManualTradePanel account={account.id} code={item.code} position={item.position}
          verified={verified} tick={tick} />
        <ManualExits account={account.id} code={item.code} position={item.position}
          verified={verified} tick={tick} exitDraft={exitDraft} trailing={item.trailing} />
      </div>
    </div>
  )
}

// 켜진 갭상승 계획.
function GapStatus({ account, item, gap }) {
  const { actions } = useStore()
  const one = (r, side) => (r.mode === 'price' ? `${fmt(r.price)}원` : `${side === 'stop' ? '−' : '+'}${r.pct}%`)
  const off = async () => {
    if (!window.confirm(`${item.name}: 갭상승 모드를 끌까요?`)) return
    try { await actions.clearBonusGap(account.id, item.code) } catch (e) { window.alert(String(e.message || e)) }
  }
  return (
    <div className="bonus-gap-status">
      <b>갭상승 대기</b>
      <span>
        {fmt(gap.budget)}원 · 시가 ≥ 전일 {gap.bull_open ? '종가(양봉이면 시가)' : '종가'} +{gap.gap_pct}% ·
        익절 {one(gap.take_profit, 'take_profit')}{gap.take_profit.partial ? `(절반, 나머지 고점 −${gap.take_profit.trail_pct}%)` : ''} ·
        손절 {one(gap.stop, 'stop')} · {mmdd(gap.started)} 켬 — 다음 거래일부터 매일 장 시작에 판정
      </span>
      {gap.message && <span className="muted">최근: {gap.message}</span>}
      <button className="link-btn" onClick={off}>끄기</button>
    </div>
  )
}

// 갭상승 모드 켜기. 익절·손절은 % 또는 가격(라인) — 산 뒤에는 카드의 손절·익절 폼·차트 선으로 바꾼다.
function GapForm({ account, item, tick, onDone }) {
  const { actions } = useStore()
  const [f, setF] = useState({
    budget: 1000000, gap_pct: 1, bull_open: false,
    take_profit: { mode: 'pct', pct: 10, price: 0, partial: false, trail_pct: 5 },
    stop: { mode: 'pct', pct: 3, price: 0 },
  })
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const set = (patch) => { setF((v) => ({ ...v, ...patch })); setMsg('') }
  const setRule = (side, patch) => { setF((v) => ({ ...v, [side]: { ...v[side], ...patch } })); setMsg('') }
  const ruleRow = (side, name, sign) => {
    const r = f[side]
    return (
      <div className="exit-row">
        <span className="gap-rule-name">{name}</span>
        <select value={r.mode} onChange={(e) => setRule(side, { mode: e.target.value,
          price: r.price || (tick?.price ? roundToTick(tick.price * (side === 'stop' ? 0.97 : 1.1)) : 0) })}
          aria-label={`${name} 방식`}>
          <option value="pct">매수가(평단) 대비 %</option>
          <option value="price">가격 라인</option>
        </select>
        {r.mode === 'pct' ? (
          <span className="exit-input">{sign}<input type="number" min="0.1" step="0.5" value={r.pct}
            onChange={(e) => setRule(side, { pct: e.target.value })} aria-label={`${name} 비율`} />%</span>
        ) : (
          <span className="exit-input"><input type="number" min="1" step="1" value={r.price}
            onChange={(e) => setRule(side, { price: e.target.value })} aria-label={`${name} 가격`} />원</span>
        )}
        {side === 'take_profit' && (
          <label className="checkfield">
            <input type="checkbox" checked={r.partial} onChange={(e) => setRule(side, { partial: e.target.checked })} />
            절반 익절 후 트레일링
            {r.partial && (
              <span className="exit-input">고점 −<input type="number" min="0.5" step="0.5" value={r.trail_pct}
                onChange={(e) => setRule(side, { trail_pct: e.target.value })} aria-label="트레일링 비율" />%</span>
            )}
          </label>
        )}
      </div>
    )
  }
  const submit = async () => {
    setBusy(true)
    try {
      const r = await actions.setBonusGap(account.id, item.code, {
        budget: Number(f.budget), gap_pct: Number(f.gap_pct), bull_open: f.bull_open,
        take_profit: ruleBody({ ...f.take_profit, enabled: true }),
        stop: ruleBody({ ...f.stop, enabled: true }),
      })
      if (r) onDone()
    } catch (e) {
      setMsg(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="bonus-gap-form">
      <div className="panel-title">갭상승 모드</div>
      <div className="bonus-fields">
        <label>매수 금액(원)
          <input type="number" min="10000" step="10000" value={f.budget} onChange={(e) => set({ budget: e.target.value })} />
        </label>
        <label>시가 ≥ 전일 종가 +%
          <input type="number" min="0" max="29" step="0.5" value={f.gap_pct} onChange={(e) => set({ gap_pct: e.target.value })} />
        </label>
        <label className="checkfield" title="전일 봉이 양봉(종가 > 시가)이면 전일 종가 대신 전일 시가를 기준으로 본다">
          <input type="checkbox" checked={f.bull_open} onChange={(e) => set({ bull_open: e.target.checked })} />
          전일이 양봉이면 전일 시가 기준
        </label>
      </div>
      {ruleRow('take_profit', '익절', '+')}
      {ruleRow('stop', '손절', '−')}
      <div className="exit-actions">
        <button className="save-btn" disabled={busy} onClick={submit}>갭상승 모드 켜기</button>
        <button className="ghost" disabled={busy} onClick={onDone}>닫기</button>
        {msg && <span className="input-err">{msg}</span>}
      </div>
      <div className="manual-stop-note">
        켠 날 다음 거래일부터 매일 장 시작 직후 일봉으로 시가를 확인해, 조건이 맞으면 금액만큼 시장가로 사고
        위 익절·손절 감시로 넘어갑니다. 안 맞으면 다음 거래일에 다시 봅니다(끌 때까지).
      </div>
    </div>
  )
}
