// 대금 양봉 매매·구간 매매 페이지의 종목 칩바. [매매] 화면의 칩바처럼 눌러서 카드를 보였다
// 숨겼다 한다(모바일에서 긴 카드 사이를 스크롤하지 않도록). 숨김 상태는 이 브라우저에만
// 저장한다(localStorage) — 서버 상태가 아니라 보기 설정이다.
import React, { useCallback, useState } from 'react'

const PHASE_SHORT = {
  DRAFT: '설정', WATCHING: '대기', ACCUMULATING: '1차', HOLDING: '보유',
  TRAILING: '추적', DONE: '종료', MANUAL: '수동',
}

const keyOf = (account, item) => `${account.id}:${item.code}`

function load(storageKey) {
  try { return new Set(JSON.parse(localStorage.getItem(storageKey) || '[]')) } catch { return new Set() }
}

// 숨김 집합 훅. isHidden(account, item) — 알림(notice)이 있는 카드는 숨기지 않는다.
export function useHiddenCards(storageKey) {
  const [hidden, setHidden] = useState(() => load(storageKey))
  const toggle = useCallback((account, item) => {
    setHidden((prev) => {
      const next = new Set(prev)
      const k = keyOf(account, item)
      next.has(k) ? next.delete(k) : next.add(k)
      try { localStorage.setItem(storageKey, JSON.stringify([...next])) } catch { /* 저장 불가(사생활 모드 등) */ }
      return next
    })
  }, [storageKey])
  const isHidden = useCallback((account, item) => hidden.has(keyOf(account, item)) && !item.notice,
    [hidden])
  return { isHidden, toggle }
}

// rows: [{account, item}], showAccount: 계좌가 여럿이면 칩에 계좌 이름을 붙인다.
export default function TradeChipBar({ rows, isHidden, toggle, showAccount }) {
  if (rows.length === 0) return null
  return (
    <div className="chip-bar trade-chip-bar">
      {rows.map(({ account, item }) => {
        const off = isHidden(account, item)
        return (
          <button
            key={keyOf(account, item)}
            className={`stock-chip ${off ? 'off' : 'on'}`}
            onClick={() => toggle(account, item)}
            aria-pressed={!off}
            title={`${showAccount ? `${account.label} · ` : ''}${off ? '클릭하면 카드 표시' : '클릭하면 카드 숨김'}`}
          >
            <span className={`chip-state chip-phase-${item.phase.toLowerCase()}`}>
              {PHASE_SHORT[item.phase] || item.phase}
            </span>
            {showAccount && <span className="chip-acc">{account.label}</span>}
            {item.name || item.code}
          </button>
        )
      })}
    </div>
  )
}
