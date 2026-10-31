// 차트별 표시 선택을 브라우저에 기억한다. 화면을 옮겼다 돌아와도 고른 대로 연다.
// - 봉 단위(3분·일봉 등)
// - 엔벨로프 켜고 끄기
// key: 차트 자리(예: 'manual:mock:005930' — 화면·계좌·종목). 선택마다 모든 자리를 한 항목(JSON)에
// 담고, 최근에 고른 것부터 MAX_ENTRIES개만 남긴다(지운 종목의 값이 끝없이 쌓이지 않게).
import { useState } from 'react'
import { VALID_INTERVALS } from './components/IntervalSelector'

const MAX_ENTRIES = 200

const readAll = (storeKey) => {
  try { return JSON.parse(localStorage.getItem(storeKey) || '{}') || {} } catch { return {} }
}

function useChartPref(storeKey, key, fallback, valid) {
  const [value, setValue] = useState(() => {
    const saved = readAll(storeKey)[key]
    return valid(saved) ? saved : fallback
  })
  const choose = (v) => {
    setValue(v)
    try {
      const all = readAll(storeKey)
      delete all[key]
      all[key] = v // 맨 뒤 = 가장 최근
      const keys = Object.keys(all)
      keys.slice(0, Math.max(0, keys.length - MAX_ENTRIES)).forEach((k) => { delete all[k] })
      localStorage.setItem(storeKey, JSON.stringify(all))
    } catch { /* 저장 불가면 이번 화면만 */ }
  }
  return [value, choose]
}

export function useChartInterval(key, fallback = 1440) {
  return useChartPref('bach.chartInterval', key, fallback, (v) => VALID_INTERVALS.includes(v))
}

// 기본은 꺼짐.
export function useChartEnvelope(key) {
  return useChartPref('bach.chartEnvelopeByChart', key, false, (v) => typeof v === 'boolean')
}
