// [상한가 매매] 자동매매 중인 종목의 시나리오·분할 매수 계획·청산 기준(서버 UlcEngine.view).
// 차트에 그릴 가격선(ulcLines)도 같은 값에서 만든다.
import React from 'react'

const won = (n) => Math.round(Number(n || 0)).toLocaleString('ko-KR')
const pct = (v) => `${+(Number(v) * 100).toFixed(2)}%`

const PHASES = {
  INIT: '진입 판단 전',
  SKIPPED: '진입 안 함(사유는 로그)',
  ACCUMULATING: '분할 매수 중',
  HOLDING: '매수 완료 · 청산 대기',
  TRAILING: '반익절 후 트레일링',
  DONE: '청산 완료',
}

export function ulcLines(engine) {
  if (!engine || !engine.scenario) return null
  const lines = [{ price: engine.x, color: '#9e9e9e', title: 'X 전일', dashed: true }]
  engine.legs.filter((l) => !l.filled && l.no > 1).forEach((l) =>
    lines.push({ price: l.target, color: '#6a1b9a', title: `${l.no}차 ≤`, dashed: true }))
  if (engine.avg_cost > 0) lines.push({ price: engine.avg_cost, color: '#455a64', title: '평단' })
  // 익절·보장 익절·켜진 손절은 시점별 계단선(ulcSteps)으로 그린다. 기록이 없는 옛 서버면 지금 값 가로선.
  const stepped = (engine.exit_marks || []).length > 0
  if (!stepped && engine.take_profit) lines.push({ price: engine.take_profit, color: '#d32f2f', title: engine.take_profit_half ? '익절(절반)' : '익절', dashed: true })
  if (!stepped && engine.guarantee) lines.push({ price: engine.guarantee, color: '#d32f2f', title: '보장 익절', dashed: true })
  if (engine.stop && !(stepped && engine.stop_active)) {
    // 분할 매수 전의 손절선은 아직 안 쓰는 예정값이라 지금 값 하나만 보인다.
    lines.push({ price: engine.stop, color: '#1565c0', title: engine.stop_active ? '손절' : '손절(분할 후)', dashed: true })
  }
  return lines
}

// 시점별 익절·손절선(계단). 서버가 기준이 바뀔 때마다 남긴 exit_marks [{t, tp, stop, stop_active, guarantee}]
// 에서 만든다 — 평단이 바뀌기 전 봉을 지금 기준과 비교하는 것처럼 보이지 않게(2026-11-23 18064K).
// 손절은 실제로 켜진(분할 매수 완료) 구간만 그린다.
export function ulcSteps(engine) {
  const marks = engine?.exit_marks || []
  if (!marks.length) return null
  const step = (key, pick) => ({ key, points: marks.map((m) => ({ t: m.t, price: pick(m) || null })) })
  return [
    { ...step('tp', (m) => m.tp), color: '#d32f2f', title: engine.take_profit_half ? '익절(절반)' : '익절', dashed: true },
    { ...step('guarantee', (m) => m.guarantee), color: '#d32f2f', title: '보장 익절', dashed: true },
    { ...step('stop', (m) => (m.stop_active ? m.stop : null)), color: '#1565c0', title: '손절', dashed: true },
  ].filter((s) => s.points.some((p) => p.price))
}

export default function UlcPlan({ engine, config }) {
  if (!engine) return null
  if (!engine.scenario) {
    return (
      <div className="ulc-plan">
        <span className="ulc-sc none">{PHASES[engine.phase] || engine.phase}</span>
        <span className="muted">X {won(engine.x)} · Z {won(engine.z)} — 사유는 로그를 보세요</span>
      </div>
    )
  }
  const gap = engine.x > 0 ? (engine.z / engine.x - 1) * 100 : 0
  return (
    <div className="ulc-plan">
      <div className="ulc-plan-row">
        <span className={`ulc-sc sc${engine.scenario}`} title="SC1: 시가 갭 < p · SC2: p ≤ 갭 < p1 · SC3: 갭 ≥ p1 (설정의 시나리오 경계)">
          SC{engine.scenario}
        </span>
        <span className="ulc-phase">{PHASES[engine.phase] || engine.phase}</span>
        <span className="muted">
          X {won(engine.x)} · Z {won(engine.z)} (시가 갭 {gap >= 0 ? '+' : ''}{gap.toFixed(2)}%)
          {engine.limit_up ? ` · 상한가 ${won(engine.limit_up)}` : ''}
        </span>
      </div>
      {engine.phase !== 'SKIPPED' && <div className="ulc-plan-row ulc-legs">
        {engine.legs.map((l) => (
          <span key={l.no} className={`ulc-leg${l.filled ? ' filled' : ''}`}>
            {l.no}차 {l.filled ? '✓ 체결' : l.no === 1 ? `시가 매수 (${won(l.target)})` : `≤ ${won(l.target)} 지정가`}
            {!l.filled && l.pending && <span className="muted"> · 주문 중</span>}
            {!l.filled && l.bought > 0 && <span className="muted"> · {won(l.bought)}주 체결</span>}
            <span className="muted"> {won(l.amount)}원</span>
          </span>
        ))}
      </div>}
      {engine.avg_cost > 0 && (
        <div className="ulc-plan-row">
          <span>평단 <strong>{won(engine.avg_cost)}</strong> · {won(engine.shares)}주</span>
          {engine.take_profit && <span className="up">익절 ≥ {won(engine.take_profit)}{engine.take_profit_half ? '(절반 후 트레일링)' : '(전량)'}</span>}
          {engine.guarantee && <span className="up">보장 익절 ≥ {won(engine.guarantee)} · 트레일링 고점 −{pct(engine.trailing_pct)}</span>}
          {engine.stop && (
            <span className="down">
              손절 ≤ {won(engine.stop)}{engine.stop_active ? '' : '(분할 매수 완료 후)'}
              {config?.ulc_stop_requires_below_prev_close ? ' · 전일 종가 아래일 때만' : ''}
              {config?.ulc_manual_on_stop ? ' · 자동 매도 없이 수동 인계' : ''}
            </span>
          )}
        </div>
      )}
    </div>
  )
}
