import React, { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { MA_LINES } from '../indicators'
import Chart from './Chart'

export default function ScreenerChart({ stock, source, onClose }) {
  const dialogRef = useRef(null)
  const [interval, setInterval] = useState(3)
  const [account, setAccount] = useState(null)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)

  useEffect(() => {
    const dialog = dialogRef.current
    dialog.showModal()
    return () => dialog.close()
  }, [])

  useEffect(() => {
    let cancelled = false
    setError('')
    setAccount(null)
    api.accounts().then((accounts) => {
      if (cancelled) return
      const candidates = accounts.filter((a) => source === 'mock' ? !a.live : a.live)
      const selected = candidates.find((a) => a.danger) || candidates[0]
      if (!selected) {
        setError(source === 'mock' ? '합성 차트를 조회할 데모 계좌가 없습니다.' : '차트 조회에 사용할 키움 계좌 연결이 필요합니다.')
        return
      }
      setAccount(selected)
    }).catch((e) => { if (!cancelled) setError(e.message) })
    return () => { cancelled = true }
  }, [source, retry])

  return (
    <dialog ref={dialogRef} className="screener-chart" onCancel={onClose} onClick={(e) => {
      if (e.target === dialogRef.current) {
        const rect = dialogRef.current.getBoundingClientRect()
        if (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom) onClose()
      }
    }} aria-labelledby="screener-chart-title">
      <header className="screener-chart-header">
        <h2 id="screener-chart-title">{stock.name || stock.code} <small>{stock.code}</small></h2>
        <button onClick={onClose} aria-label="차트 닫기" autoFocus>닫기</button>
      </header>
      <div className="screener-chart-tools" aria-label="차트 주기">
        {[3, 1440].map((value) => <button key={value} aria-pressed={interval === value} onClick={() => setInterval(value)}>{value === 3 ? '3분봉' : '일봉'}</button>)}
        {MA_LINES.map((line) => <span key={line.period} style={{ color: line.color }}>MA{line.period}</span>)}
        <span>거래량</span>
      </div>
      <p className="muted">{source === 'mock' ? '합성 데모 차트' : '키움 최신 시세 차트'} · 목록 조회일과 별개로 최신 봉을 표시합니다.</p>
      {error ? <p role="alert">{error} <button onClick={() => setRetry((value) => value + 1)}>다시 시도</button></p>
        : account ? <Chart key={`${stock.code}:${interval}`} account={account.id} code={stock.code} interval={interval} height={460} />
          : <p role="status">시세 연결 확인 중…</p>}
    </dialog>
  )
}
