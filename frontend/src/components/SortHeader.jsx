// 스크리너 표의 '큰 값 먼저' 정렬. 열 머리를 누르면 그 열 기준으로 내림차순 정렬한다.
import React from 'react'

// keys 순서대로 비교해 큰 값이 위로 온다(앞 키가 같으면 다음 키로 가른다).
export function sortDesc(rows, keys) {
  return [...rows].sort((a, b) => {
    for (const k of keys) {
      const diff = (b[k] || 0) - (a[k] || 0)
      if (diff) return diff
    }
    return 0
  })
}

export default function SortHeader({ sortKey, active, onSort, title, children }) {
  const on = active === sortKey
  return (
    <th className="col-num" title={title} aria-sort={on ? 'descending' : 'none'}>
      <button type="button" className={`th-sort${on ? ' active' : ''}`} onClick={() => onSort(sortKey)}>
        {children}<span className="th-sort-mark" aria-hidden="true">{on ? '▼' : '↕'}</span>
      </button>
    </th>
  )
}
