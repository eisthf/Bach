import React from 'react'

const fmt = (n) => (n == null ? '-' : Number(n).toLocaleString('ko-KR'))

// 장중 고가/저가/현재가 실시간 표시.
export default function PriceTicker({ tick }) {
  const price = tick?.price
  const open = tick?.open
  // 시가 대비 등락으로 현재가 색상 결정
  const up = price != null && open != null && price >= open
  return (
    <div className="ticker">
      <div className="ticker-cell">
        <span className="ticker-label">현재가</span>
        <span className={`ticker-price ${up ? 'up' : 'down'}`}>{fmt(price)}</span>
      </div>
      <div className="ticker-cell">
        <span className="ticker-label">고가</span>
        <span className="ticker-val up">{fmt(tick?.high)}</span>
      </div>
      <div className="ticker-cell">
        <span className="ticker-label">저가</span>
        <span className="ticker-val down">{fmt(tick?.low)}</span>
      </div>
    </div>
  )
}
