// 단순이동평균(SMA) 계산.
// 이전 60봉을 포함한 전체 배열로 계산해야 당일 첫 봉의 SMA60 값이 채워진다.

// bars: [{time, open, high, low, close, ...}] (시간순)
// period: 이평 기간
// 반환: [{time, value}] — 데이터가 부족한 앞 구간은 생략(라인이 자연히 끊김)
export function sma(bars, period) {
  const out = []
  let sum = 0
  for (let i = 0; i < bars.length; i++) {
    sum += bars[i].close
    if (i >= period) sum -= bars[i - period].close
    if (i >= period - 1) {
      out.push({ time: bars[i].time, value: sum / period })
    }
  }
  return out
}

// 이평선 정의 (요구사항 색상)
export const MA_LINES = [
  { period: 5, color: '#2962FF', title: 'MA5' },   // 파랑
  { period: 10, color: '#FF4FA3', title: 'MA10' }, // 분홍
  { period: 20, color: '#FF9800', title: 'MA20' }, // 주황
  { period: 60, color: '#26A65B', title: 'MA60' }, // 초록
]
