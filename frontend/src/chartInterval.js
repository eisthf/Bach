// 차트 봉 단위(3분·일봉 등) 선택을 브라우저에 기억한다. 화면을 옮겼다 돌아와도 고른 봉으로 연다.
// key: 차트 자리(예: 'manual:mock:005930' — 화면·계좌·종목). 모든 자리를 한 항목(JSON)에 담고,
// 최근에 고른 것부터 MAX_ENTRIES개만 남긴다(지운 종목의 값이 끝없이 쌓이지 않게).
import { useState } from 'react'
import { VALID_INTERVALS } from './components/IntervalSelector'

const STORE_KEY = 'bach.chartInterval'
const MAX_ENTRIES = 200

const readAll = () => {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || '{}') || {} } catch { return {} }
}

export function useChartInterval(key, fallback = 1440) {
  const [interval, setIntervalState] = useState(() => {
    const saved = readAll()[key]
    return VALID_INTERVALS.includes(saved) ? saved : fallback
  })
  const choose = (v) => {
    setIntervalState(v)
    try {
      const all = readAll()
      delete all[key]
      all[key] = v // 맨 뒤 = 가장 최근
      const keys = Object.keys(all)
      keys.slice(0, Math.max(0, keys.length - MAX_ENTRIES)).forEach((k) => { delete all[k] })
      localStorage.setItem(STORE_KEY, JSON.stringify(all))
    } catch { /* 저장 불가면 이번 화면만 */ }
  }
  return [interval, choose]
}
