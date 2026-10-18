// 수동 매매의 자동 손절·익절 설정 + 차트 선. 각각 두 방식:
//   (1) 평단 대비 %: 숫자로 지정 — 손절 평단 −x% 이하, 익절 평단 +y% 이상
//   (2) 라인: 차트에 수평선을 그어 지정([차트에 긋기] 후 차트 클릭, 끌어서 옮기기) — 손절 라인 미만, 익절 라인 이상
// 조건이 되면 서버가 보유 전량을 시장가로 한 번 팔고 둘 다 끈다(backend/app/manual_trade.py).
// 설정은 카드가 '초안(draft)'으로 들고 있다 — 폼과 차트 선이 같은 초안을 보고, 차트의 선을 끌면
// 그 가격의 라인 방식으로 바뀐다. 긋는 중(pick)인 쪽도 초안이 들고 있다. [저장]해야 서버에 반영된다.
import React, { useEffect, useMemo, useState } from 'react'
import { useStore } from '../store'
import { roundToTick } from '../candleZone'

const fmt = (n) => Math.round(Number(n || 0)).toLocaleString('ko-KR')
// 화면 순서: 익절(위) → 손절(아래). 차트에서 익절선이 손절선보다 위에 있는 것과 맞춘다.
const SIDES = [
  { key: 'take_profit', name: '익절', sign: '+', color: '#2e7d32', defaultPct: 10 },
  { key: 'stop', name: '손절', sign: '−', color: '#d32f2f', defaultPct: 5 },
]
const ruleOf = (r, pct) => ({ enabled: !!r?.enabled, mode: r?.mode || 'pct', pct: r?.pct ?? pct, price: r?.price || 0 })
const fromItem = (exits) => ({
  stop: ruleOf(exits?.stop, 5),
  take_profit: ruleOf(exits?.take_profit, 10),
})

// 발동 가격. 비율 방식은 평단이 있어야 정해진다.
export function exitLevel(rule, avg, side) {
  if (rule.mode === 'price') return Number(rule.price) > 0 ? Number(rule.price) : null
  const pct = Number(rule.pct)
  if (!(avg > 0) || !(pct > 0)) return null
  return side === 'stop' ? avg * (1 - pct / 100) : avg * (1 + pct / 100)
}

// 현재가가 이미 조건을 넘었는가(저장하면 정규장에서 곧바로 팔린다).
function crossed(rule, level, price, side) {
  if (!rule.enabled || level == null || !(price > 0)) return false
  if (side === 'take_profit') return price >= level
  return rule.mode === 'price' ? price < level : price <= level
}

// 카드가 쓰는 초안 훅. 서버 값이 바뀌면(다른 화면·자동 해제) 편집 중이 아닐 때만 따라간다.
export function useExitDraft(item) {
  const serverKey = JSON.stringify(item.exits || {})
  const [draft, setDraft] = useState(() => fromItem(item.exits))
  const [dirty, setDirty] = useState(false)
  const [pick, setPick] = useState(null)  // 차트에 긋는 중인 쪽('stop' | 'take_profit')
  useEffect(() => {
    if (!dirty) setDraft(fromItem(item.exits))
  }, [serverKey]) // eslint-disable-line react-hooks/exhaustive-deps
  const update = (side, patch) => {
    setDraft((d) => ({ ...d, [side]: { ...d[side], ...patch } }))
    setDirty(true)
  }
  const reset = () => {
    setDraft(fromItem(item.exits))
    setDirty(false)
    setPick(null)
  }
  // 차트를 눌러 긋는다(호가 단위로 맞춤).
  const drawAt = (price) => {
    if (!pick) return
    update(pick, { mode: 'price', price: roundToTick(price), enabled: true })
    setPick(null)
  }
  return { draft, dirty, update, reset, setDirty, pick, setPick, drawAt }
}

// 차트에 넘길 선(켜진 것만) + 평단선. 손절·익절 선은 끌 수 있다.
export function useExitLines(draft, avg) {
  return useMemo(() => {
    const lines = []
    const drag = []
    if (avg > 0) lines.push({ price: avg, color: '#888', title: '평단', dashed: true })
    SIDES.forEach((s) => {
      const rule = draft[s.key]
      const level = exitLevel(rule, avg, s.key)
      if (!rule.enabled || level == null) return
      const title = rule.mode === 'price' ? `${s.name} 라인` : `${s.name} ${s.sign}${rule.pct}%`
      lines.push({ price: level, color: s.color, title, dragId: s.key })
      drag.push({ id: s.key, price: level })
    })
    return { lines, drag }
  }, [draft, avg])
}

export default function ManualExits({ account, code, position, verified, tick, exitDraft }) {
  const { actions } = useStore()
  const { draft, dirty, update, reset, setDirty, pick, setPick } = exitDraft
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const avg = verified ? Number(position?.avg_price || 0) : 0
  const price = tick?.price

  // 라인 방식으로 바꿀 때 아직 그은 선이 없으면 바로 긋기 모드로 들어간다.
  const setMode = (s, mode) => {
    update(s.key, { mode })
    if (mode === 'price' && !(Number(draft[s.key].price) > 0)) setPick(s.key)
    else if (pick === s.key) setPick(null)
  }

  const problems = SIDES.flatMap((s) => {
    const r = draft[s.key]
    if (!r.enabled) return []
    if (r.mode === 'price' && !(Number(r.price) > 0)) return [`차트에 ${s.name} 라인을 그으세요.`]
    const high = s.key === 'stop' ? 100 : 1000
    if (r.mode === 'pct' && !(Number(r.pct) > 0 && Number(r.pct) < high)) return [`${s.name} 비율은 0 초과 ${high}% 미만이어야 합니다.`]
    return []
  })
  const lo = draft.stop.enabled ? exitLevel(draft.stop, avg, 'stop') : null
  const hi = draft.take_profit.enabled ? exitLevel(draft.take_profit, avg, 'take_profit') : null
  if (lo != null && hi != null && lo >= hi) problems.push('손절 가격은 익절 가격보다 낮아야 합니다.')

  const save = async () => {
    const hits = SIDES.filter((s) => crossed(draft[s.key], exitLevel(draft[s.key], avg, s.key), price, s.key))
    if (hits.length && !window.confirm(
      `현재가 ${fmt(price)}원이 이미 ${hits.map((s) => s.name).join('·')} 조건을 넘었습니다.\n`
      + '저장하면 정규장 중 곧바로 보유 전량이 시장가로 매도됩니다. 저장할까요?')) return
    setBusy(true)
    try {
      const body = (r) => ({ enabled: r.enabled, mode: r.mode, pct: Number(r.pct), price: Number(r.price) || 0 })
      await actions.setManualExits(account, code, body(draft.stop), body(draft.take_profit))
      setDirty(false)
      setMsg('자동 손절·익절 저장됨')
    } catch (e) {
      setMsg(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="manual-exits">
      <div className="panel-title">자동 손절·익절</div>
      {SIDES.map((s) => {
        const r = draft[s.key]
        const level = exitLevel(r, avg, s.key)
        const cmp = s.key === 'take_profit' ? '이상' : r.mode === 'price' ? '미만' : '이하'
        return (
          <div key={s.key} className={`exit-row exit-${s.key}`}>
            <label className="checkfield">
              <input type="checkbox" checked={r.enabled} onChange={(e) => update(s.key, { enabled: e.target.checked })} />
              <span style={{ color: s.color }}>{s.name}</span>
            </label>
            <select value={r.mode} onChange={(e) => setMode(s, e.target.value)} aria-label={`${s.name} 방식`}>
              <option value="pct">평단 대비 %</option>
              <option value="price">{s.name} 라인(차트)</option>
            </select>
            {r.mode === 'pct' ? (
              <span className="exit-input">
                {s.sign}<input type="number" min="0.1" step="0.1" value={r.pct}
                  onChange={(e) => update(s.key, { pct: e.target.value })} aria-label={`${s.name} 비율`} />%
              </span>
            ) : (
              <button type="button" className={`draw-btn${pick === s.key ? ' on' : ''}`}
                style={pick === s.key ? { borderColor: s.color, color: s.color } : undefined}
                onClick={() => setPick(pick === s.key ? null : s.key)}>
                {pick === s.key ? '차트를 누르세요 · 취소' : Number(r.price) > 0 ? '다시 긋기' : '차트에 긋기'}
              </button>
            )}
            <span className="hint">
              {level == null ? (r.mode === 'pct' ? '평단 확인 후 계산' : '') : `${fmt(level)}원 ${cmp}`}
              {/* 라인 방식은 그 선이 평단에서 몇 % 떨어졌는지도 보인다(비율 방식은 입력값이 곧 %). */}
              {r.mode === 'price' && level != null && avg > 0 && (
                <span className={`exit-vs-avg ${level >= avg ? 'up' : 'down'}`}>
                  {' · 평단 '}{((level - avg) / avg * 100).toLocaleString('ko-KR', { minimumFractionDigits: 1, maximumFractionDigits: 1, signDisplay: 'exceptZero' })}%
                </span>
              )}
            </span>
          </div>
        )
      })}
      <div className="exit-actions">
        <button className="save-btn" disabled={busy || !dirty || problems.length > 0} onClick={save}>저장</button>
        {dirty && <button className="ghost" disabled={busy} onClick={() => { reset(); setMsg('') }}>되돌리기</button>}
        {dirty && <span className="exit-dirty">저장 안 됨</span>}
      </div>
      {problems.length > 0 && <div className="input-err">{problems[0]}</div>}
      <div className="manual-stop-note">
        정규장 체결가로 판단 · 조건 충족 시 보유 전량 시장가 매도 후 손절·익절 모두 해제 ·
        라인은 [차트에 긋기] 후 차트를 눌러 긋고, 그은 선은 끌어서 옮깁니다(비율 선을 끌어도 라인으로 바뀜)
      </div>
      {msg && <div className="trade-msg">{msg}</div>}
    </div>
  )
}
