import React from 'react'
import { ROUTES, navigate, useRoute } from '../router'

// 매매 ↔ 상한가 종목 페이지 전환. 계좌 필터와 같은 세그먼트 형태.
const PAGES = [
  { path: ROUTES.TRADING, label: '매매' },
  { path: ROUTES.UPPER_LIMIT, label: '상한가 종목' },
]

export default function PageNav() {
  const route = useRoute()
  return (
    <nav className="page-nav" role="group" aria-label="페이지">
      {PAGES.map((p) => (
        <button
          key={p.path}
          className={`page-tab${route === p.path ? ' active' : ''}`}
          aria-current={route === p.path ? 'page' : undefined}
          onClick={() => navigate(p.path)}
        >
          {p.label}
        </button>
      ))}
    </nav>
  )
}
