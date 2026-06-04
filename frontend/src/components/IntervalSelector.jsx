import React from 'react'

const INTERVALS = [3, 5, 10, 30, 60]

export default function IntervalSelector({ value, onChange }) {
  return (
    <div className="interval-selector">
      {INTERVALS.map((iv) => (
        <button
          key={iv}
          className={`iv-btn ${value === iv ? 'active' : ''}`}
          onClick={() => onChange(iv)}
        >
          {iv}분
        </button>
      ))}
    </div>
  )
}
