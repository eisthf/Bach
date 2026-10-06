// 스크리너 표의 열 정렬. 열 머리를 누르면 그 열 기준 내림차순(큰 값 먼저)으로 정렬하고,
// 같은 열을 다시 누르면 오름차순·내림차순을 번갈아 바꾼다.
import { useState } from 'react'
import React from 'react'

// 정렬 상태 {key, dir}와 열 머리 클릭 핸들러. 새 열은 내림차순부터 시작한다.
export function useSort(initialKey) {
  const [sort, setSort] = useState({ key: initialKey, dir: 'desc' })
  const onSort = (key) => setSort((s) => (
    s.key === key ? { key, dir: s.dir === 'desc' ? 'asc' : 'desc' } : { key, dir: 'desc' }
  ))
  return [sort, onSort]
}

// keys 순서대로 비교한다(앞 키가 같으면 다음 키로 가른다). 값이 없는(0) 행은
// 정렬 방향과 상관없이 맨 아래에 둔다 — 오름차순에서 '모름'이 맨 위로 오지 않게.
export function sortRows(rows, keys, dir = 'desc') {
  const sign = dir === 'asc' ? 1 : -1
  return [...rows].sort((a, b) => {
    for (const k of keys) {
      const av = a[k] || 0
      const bv = b[k] || 0
      if (!av !== !bv) return av ? -1 : 1
      const diff = (av - bv) * sign
      if (diff) return diff
    }
    return 0
  })
}

export default function SortHeader({ sortKey, sort, onSort, title, children }) {
  const on = sort.key === sortKey
  const asc = on && sort.dir === 'asc'
  const hint = on ? (asc ? '작은 순 — 누르면 큰 순' : '큰 순 — 누르면 작은 순') : '누르면 큰 순으로 정렬'
  return (
    <th className="col-num" title={title ? `${title} (${hint})` : hint}
      aria-sort={on ? (asc ? 'ascending' : 'descending') : 'none'}>
      <button type="button" className={`th-sort${on ? ' active' : ''}`} onClick={() => onSort(sortKey)}>
        {children}<span className="th-sort-mark" aria-hidden="true">{on ? (asc ? '▲' : '▼') : '↕'}</span>
      </button>
    </th>
  )
}
