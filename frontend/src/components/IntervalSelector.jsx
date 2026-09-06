import React from 'react'

const INTERVALS = [
  { value: 3, label: '3분' },
  { value: 5, label: '5분' },
  { value: 10, label: '10분' },
  { value: 30, label: '30분' },
  { value: 60, label: '60분' },
  { value: 1440, label: '일봉' },
]

export default function IntervalSelector({ value, onChange }) {
  return (
    <div className="interval-selector">
      {INTERVALS.map((iv) => (
        <button
          key={iv.value}
          className={`iv-btn ${value === iv.value ? 'active' : ''}`}
          onClick={() => onChange(iv.value)}
        >
          {iv.label}
        </button>
      ))}
    </div>
  )
}
