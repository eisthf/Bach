import React, { useEffect, useState } from 'react'
import { useStore } from '../store'

// upper_limit_chase 파라미터 입력폼.
// 장전/MONITOR에서 편집, AUTO_TRADING 진입 시 잠금.
// 묶음(투자·진입·시나리오·익절·손절)별로 라벨 바로 옆에 입력칸을 둔다. 비율은 서버에 소수(0.05)로
// 저장하고 화면에서만 %(5)로 보이고 받는다.
const toPct = (v) => (v === '' || v == null || Number.isNaN(Number(v)) ? '' : +(Number(v) * 100).toFixed(4))
const fmt = (n) => Number(n || 0).toLocaleString('ko-KR')

export default function AutoConfigForm({ account, stock }) {
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
    await actions.putConfig(account, stock.code, cfg)
    setSaved(true)
  }

  // % 입력칸: 화면 값 = 저장값 × 100
  const pct = (key, extra = {}) => (
    <input type="number" step="0.1" min="0" value={toPct(cfg[key])} disabled={locked || extra.disabled}
      onChange={(e) => setField(key, e.target.value === '' ? '' : Number(e.target.value) / 100)} />
  )
  const check = (key, label, title) => (
    <label className="checkfield" title={title}>
      <input type="checkbox" checked={!!cfg[key]} disabled={locked}
        onChange={(e) => setField(key, e.target.checked)} />
      <span>{label}</span>
    </label>
  )
  const p = toPct(cfg.ulc_p)
  const p1 = toPct(cfg.ulc_p1)
  const trailingOff = !cfg.ulc_trailing

  return (
    <div className={`auto-form ${locked ? 'locked' : ''}`}>
      <div className="panel-title">자동매매 설정</div>
      <div className="ulc-cfg">
        <fieldset>
          <legend>투자</legend>
          <label className="cfg-row">총 투자액
            <input type="number" step="100000" min="0" className="wide" value={cfg.max_buy_amount} disabled={locked}
              onChange={(e) => setField('max_buy_amount', Number(e.target.value))} />원
          </label>
          {check('ulc_first_buy_only', '1차 매수만 (추가 매수 없음)', '시나리오의 2차·3차 매수를 하지 않습니다. 1차에 쓰는 금액은 그대로입니다.')}
        </fieldset>

        <fieldset>
          <legend>진입</legend>
          <label className="cfg-row" title="당일 시가가 전일 종가(X)보다 이만큼 이상 높으면 매매하지 않습니다.">
            시가 갭이 {pct('ulc_w')}% 이상이면 진입 안 함
          </label>
          {check('ulc_allow_lower_open', '시가가 전일 종가 아래여도 진입(SC1)')}
          <label className="cfg-row">1차 매수 주문
            <select value={cfg.ulc_first_buy_ask3 ? 'ask3' : 'market'} disabled={locked}
              onChange={(e) => setField('ulc_first_buy_ask3', e.target.value === 'ask3')}>
              <option value="ask3">매도 3호가 지정가</option>
              <option value="market">시장가</option>
            </select>
          </label>
          {check('use_3min_bar_timing', '3분봉 종가로 판단 (틱 대신)', '완성된 3분봉의 종가로만 매수·청산 조건을 봅니다.')}
        </fieldset>

        <fieldset>
          <legend>시나리오 <span className="muted">(시가 갭 = 시가 ÷ 전일 종가 − 1)</span></legend>
          <div className="cfg-sc">
            <div className="cfg-row"><b className="sc-tag sc1">SC1</b> 갭 {pct('ulc_p')}% 미만</div>
            <div className="cfg-row cfg-sub">2분할 · 2차 = 전일 종가 −{pct('ulc_q')}%</div>
          </div>
          <div className="cfg-sc">
            <div className="cfg-row"><b className="sc-tag sc2">SC2</b> 갭 {p}% 이상 {pct('ulc_p1')}% 미만</div>
            <div className="cfg-row cfg-sub muted">2분할 · 2차 = 전일 종가</div>
          </div>
          <div className="cfg-sc">
            <div className="cfg-row"><b className="sc-tag sc3">SC3</b> 갭 {p1}% 이상</div>
            <div className="cfg-row cfg-sub muted">3분할 · 2차 = 시가 −5%, 3차 = 시가 −9.75%</div>
          </div>
        </fieldset>

        <fieldset>
          <legend>익절</legend>
          <label className="cfg-row">평단 +{pct('ulc_tp')}% 에서 {cfg.ulc_trailing ? '절반 익절' : '전량 익절'}</label>
          {check('ulc_trailing', '절반 익절 후 나머지 트레일링')}
          <label className={`cfg-row cfg-sub${trailingOff ? ' off' : ''}`}>트레일링: 고점 −{pct('ulc_t', { disabled: trailingOff })}% 에서 매도</label>
          <label className={`cfg-row cfg-sub${trailingOff ? ' off' : ''}`}>보장 익절: 평단 +{pct('ulc_g', { disabled: trailingOff })}% 에서 전량</label>
          <div className="cfg-note muted">상한가에 닿으면 전량 매도</div>
        </fieldset>

        <fieldset>
          <legend>손절</legend>
          <label className="cfg-row" title="분할 매수를 모두 마친 뒤부터 봅니다.">평단 −{pct('ulc_sl')}% 에서 손절
            <span className="muted">(분할 매수 완료 후)</span>
          </label>
          {check('ulc_stop_requires_below_prev_close', '전일 종가 아래일 때만 손절')}
          {check('ulc_manual_on_stop', '자동 매도 없이 수동 매매로 인계')}
        </fieldset>
      </div>
      <div className="auto-actions">
        <button disabled={locked} onClick={save} className="save-btn">
          설정 저장
        </button>
        {saved && <span className="saved-note">저장됨 ✓</span>}
        {locked && <span className="lock-note">자동매매 중 — 설정 잠금</span>}
        {!locked && <span className="muted">총 {fmt(cfg.max_buy_amount)}원</span>}
      </div>
    </div>
  )
}
