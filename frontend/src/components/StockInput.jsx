import React, { useState } from 'react'
import { useStore } from '../store'

// 종목코드 입력 → 추가. 하나 이상 추가 가능(엔터/버튼).
export default function StockInput() {
  const { actions } = useStore()
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [err, setErr] = useState('')

  const submit = async (e) => {
    e.preventDefault()
    const c = code.trim()
    if (!c) return
    try {
      await actions.addStock(c, name.trim())
      setCode('')
      setName('')
      setErr('')
    } catch (e) {
      setErr(String(e.message || e))
    }
  }

  return (
    <form className="stock-input" onSubmit={submit}>
      <input
        className="code-input"
        placeholder="종목코드 (예: 005930)"
        value={code}
        onChange={(e) => setCode(e.target.value)}
      />
      <input
        className="name-input"
        placeholder="종목명 (선택)"
        value={name}
        onChange={(e) => setName(e.target.value)}
      />
      <button type="submit" className="add-btn">+ 종목 추가</button>
      {err && <span className="input-err">{err}</span>}
    </form>
  )
}
