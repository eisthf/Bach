import React, { useEffect, useState } from 'react'
import { useStore } from '../store'

// upper_limit_chase 파라미터 입력폼.
// 장전/MONITOR에서 편집, AUTO_TRADING 진입 시 잠금.
const FIELDS = [
  { key: 'max_buy_amount', label: '총 투자액(원)', step: 100000, group: '금액' },
  { key: 'ulc_p', label: 'SC1/2 경계(p)', step: 0.01, group: '시나리오' },
  { key: 'ulc_p1', label: 'SC2/3 경계(p1)', step: 0.01, group: '시나리오' },
  { key: 'ulc_q', label: 'SC1 2차가(q)', step: 0.01, group: '시나리오' },
  { key: 'ulc_w', label: '갭 상한(w)', step: 0.01, group: '진입필터' },
  { key: 'ulc_tp', label: '익절(tp)', step: 0.01, group: '청산' },
  { key: 'ulc_sl', label: '손절(sl)', step: 0.01, group: '청산' },
  { key: 'ulc_t', label: '트레일링(t)', step: 0.01, group: '트레일링' },
  { key: 'ulc_g', label: '보장익절(g)', step: 0.01, group: '트레일링' },
]
const BOOLS = [
  { key: 'ulc_allow_lower_open', label: '하락 시가 진입 허용' },
  { key: 'ulc_trailing', label: '트레일링 스탑' },
  { key: 'ulc_first_buy_only', label: '1차 매수만' },
  { key: 'use_3min_bar_timing', label: '3분봉 타이밍' },
]

export default function AutoConfigForm({ stock }) {
  const { actions } = useStore()
  const [cfg, setCfg] = useState(stock.config)
  const [saved, setSaved] = useState(false)
  const locked = stock.state === 'AUTO_TRADING'

  // 서버 상태가 바뀌면(다른 곳에서 갱신) 반영
  useEffect(() => {
    setCfg(stock.config)
  }, [stock.config])

  const setField = (k, v) => {
    setCfg((c) => ({ ...c, [k]: v }))
    setSaved(false)
  }
  const save = async () => {
    await actions.putConfig(stock.code, cfg)
    setSaved(true)
  }

  return (
    <div className={`auto-form ${locked ? 'locked' : ''}`}>
      <div className="panel-title">자동매매 설정</div>
      <div className="auto-grid">
        {FIELDS.map((f) => (
          <label key={f.key} className="field">
            <span>{f.label}</span>
            <input
              type="number"
              step={f.step}
              value={cfg[f.key]}
              disabled={locked}
              onChange={(e) => setField(f.key, Number(e.target.value))}
            />
          </label>
        ))}
      </div>
      <div className="auto-bools">
        {BOOLS.map((b) => (
          <label key={b.key} className="checkfield">
            <input
              type="checkbox"
              checked={!!cfg[b.key]}
              disabled={locked}
              onChange={(e) => setField(b.key, e.target.checked)}
            />
            <span>{b.label}</span>
          </label>
        ))}
      </div>
      <div className="auto-actions">
        <button disabled={locked} onClick={save} className="save-btn">
          설정 저장
        </button>
        {saved && <span className="saved-note">저장됨 ✓</span>}
        {locked && <span className="lock-note">자동매매 중 — 설정 잠금</span>}
      </div>
    </div>
  )
}
