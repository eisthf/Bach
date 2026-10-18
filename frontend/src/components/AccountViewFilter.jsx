// 대금 양봉 매매·구간 매매 페이지의 계좌 보기 필터(세그먼트). [매매] 화면의 계좌 필터와 모양은 같지만
// 페이지마다 따로 기억한다(이 브라우저 localStorage) — 다른 화면에서 고른 계좌 때문에 실전 매매가
// 조용히 가려지지 않게, 처음엔 '모두'다. 종목 추가 폼의 계좌 선택과는 별개다.
import React, { useCallback, useState } from 'react'

const ALL = 'ALL'

export function useAccountView(storageKey, accounts) {
  const [view, setView] = useState(() => {
    try { return localStorage.getItem(storageKey) || ALL } catch { return ALL }
  })
  const choose = useCallback((id) => {
    setView(id)
    try { localStorage.setItem(storageKey, id) } catch { /* 저장 불가(사생활 모드 등) */ }
  }, [storageKey])
  // 저장된 계좌가 없어졌으면(설정 변경) 모두로.
  const current = view === ALL || accounts.some((a) => a.id === view) ? view : ALL
  const visible = useCallback((account) => current === ALL || account.id === current, [current])
  return { view: current, choose, visible }
}

// rows: [{account, item}] — 탭마다 그 계좌 종목 수를 붙인다. 계좌가 하나면 숨긴다.
export default function AccountViewFilter({ accounts, rows, view, choose }) {
  if (accounts.length < 2) return null
  const count = (id) => rows.filter(({ account }) => id === ALL || account.id === id).length
  const tabs = [{ id: ALL, label: '모두' }, ...accounts]
  const hidden = view === ALL ? 0 : rows.length - count(view)
  return (
    <div className="page-account-filter">
      <span className="muted">계좌 보기</span>
      <div className="account-filter" role="group" aria-label="계좌 보기">
        {tabs.map((t) => (
          <button
            key={t.id}
            className={`acc-tab${view === t.id ? ' active' : ''}${t.danger ? ' danger' : ''}`}
            onClick={() => choose(t.id)}
            aria-pressed={view === t.id}
          >
            {t.label} <span className="acc-count">{count(t.id)}</span>
          </button>
        ))}
      </div>
      {hidden > 0 && (
        <button className="link-btn" onClick={() => choose(ALL)}>다른 계좌 {hidden}건 가려짐 · 모두 보기</button>
      )}
    </div>
  )
}
