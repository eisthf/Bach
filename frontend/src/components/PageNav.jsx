import React from 'react'
import { ROUTES, navigate, useRoute } from '../router'

// 페이지 전환. 성격이 다른 두 묶음을 나눠 보인다:
//   조회 — 시장 전체 스크리너·지난 매매 기록(주문 없음)
//   매매 — 계좌에서 주문이 나가는 화면. 어느 자동매매든 [수동 전환]하면 수동 매매로 모인다.
const GROUPS = [
  {
    label: '조회',
    pages: [
      { path: ROUTES.UPPER_LIMIT, label: '상한가 종목' },
      { path: ROUTES.BIG_CANDLE, label: '대금 양봉' },
      { path: ROUTES.TRADE_HISTORY, label: '매매 기록' },
    ],
  },
  {
    label: '매매',
    pages: [
      { path: ROUTES.TRADING, label: '상한가 매매' },
      { path: ROUTES.CANDLE_TRADE, label: '대금 양봉 매매' },
      { path: ROUTES.ZONE_TRADE, label: '구간 매매' },
      { path: ROUTES.BONUS_TRADE, label: '무상증자 매매' },
      { path: ROUTES.MANUAL_TRADE, label: '수동 매매' },
    ],
  },
]

export default function PageNav() {
  const route = useRoute()
  return (
    <nav className="page-nav-groups" aria-label="페이지">
      {GROUPS.map((g) => (
        <div key={g.label} className="page-nav-group">
          <span className="page-nav-label">{g.label}</span>
          <div className="page-nav" role="group" aria-label={g.label}>
            {g.pages.map((p) => (
              <button
                key={p.path}
                className={`page-tab${route === p.path ? ' active' : ''}`}
                aria-current={route === p.path ? 'page' : undefined}
                onClick={() => navigate(p.path)}
              >
                {p.label}
              </button>
            ))}
          </div>
        </div>
      ))}
    </nav>
  )
}
