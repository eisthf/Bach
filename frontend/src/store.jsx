// 전역 상태: 종목 목록/상태/실시간 시세/로그/장 단계 + WebSocket 구독.
import React, { createContext, useContext, useEffect, useRef, useState } from 'react'
import { api } from './api'

const StoreCtx = createContext(null)
export const useStore = () => useContext(StoreCtx)

export function StoreProvider({ children }) {
  // code -> StockStatus({code,name,state,config,position})
  const [stocks, setStocks] = useState({})
  // 추가된 순서(코드 배열). 객체 키 순서는 숫자형 키가 먼저 정렬되어
  // 추가 순서가 깨지므로, 명시적 순서 배열로 위→아래 배치를 보장한다.
  const [order, setOrder] = useState([])
  // code -> latest tick({code,price,high,low,open,...})
  const [ticks, setTicks] = useState({})
  const [phase, setPhase] = useState('PRE_OPEN')
  const [marketAuto, setMarketAuto] = useState(false)
  // 숨긴 종목코드 집합(목록엔 유지, 패널만 숨김). 브라우저에 기억(localStorage).
  const [hidden, setHidden] = useState(() => {
    try { return new Set(JSON.parse(localStorage.getItem('bach.hidden') || '[]')) }
    catch { return new Set() }
  })
  // 컴팩트 모드 종목코드 집합(차트+매매만, 자동매매설정 숨김). 브라우저에 기억.
  const [compact, setCompact] = useState(() => {
    try { return new Set(JSON.parse(localStorage.getItem('bach.compact') || '[]')) }
    catch { return new Set() }
  })
  const [logs, setLogs] = useState([])
  const [connected, setConnected] = useState(false)
  const wsRef = useRef(null)

  // 초기 스냅샷
  useEffect(() => {
    api.listStocks().then((list) => {
      const map = {}
      list.forEach((s) => (map[s.code] = s))
      setStocks(map)
      setOrder(list.map((s) => s.code))
    })
    api.market().then((m) => {
      setPhase(m.phase)
      setMarketAuto(!!m.auto)
    })
  }, [])

  // WebSocket 구독 (자동 재연결)
  useEffect(() => {
    let stopped = false
    function connect() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      const ws = new WebSocket(`${proto}://${location.host}/ws`)
      wsRef.current = ws
      ws.onopen = () => setConnected(true)
      ws.onclose = () => {
        setConnected(false)
        if (!stopped) setTimeout(connect, 1000)
      }
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data)
        if (msg.type === 'tick') {
          const t = msg.tick
          setTicks((prev) => ({ ...prev, [t.code]: t }))
        } else if (msg.type === 'status') {
          const s = msg.status
          setStocks((prev) => ({ ...prev, [s.code]: s }))
          setOrder((prev) => (prev.includes(s.code) ? prev : [...prev, s.code]))
        } else if (msg.type === 'market') {
          setPhase(msg.phase)
          if (msg.auto !== undefined) setMarketAuto(!!msg.auto)
        } else if (msg.type === 'log') {
          setLogs((prev) => [...prev.slice(-200), { t: Date.now(), text: msg.text }])
        }
      }
    }
    connect()
    return () => {
      stopped = true
      wsRef.current?.close()
    }
  }, [])

  // 액션들
  const actions = {
    addStock: async (code, name) => {
      const s = await api.addStock(code, name)
      setStocks((prev) => ({ ...prev, [s.code]: s }))
      setOrder((prev) => (prev.includes(s.code) ? prev : [...prev, s.code]))
    },
    removeStock: async (code) => {
      await api.removeStock(code)
      setStocks((prev) => {
        const n = { ...prev }
        delete n[code]
        return n
      })
      setOrder((prev) => prev.filter((c) => c !== code))
      setHidden((prev) => {
        if (!prev.has(code)) return prev
        const n = new Set(prev)
        n.delete(code)
        localStorage.setItem('bach.hidden', JSON.stringify([...n]))
        return n
      })
      setCompact((prev) => {
        if (!prev.has(code)) return prev
        const n = new Set(prev)
        n.delete(code)
        localStorage.setItem('bach.compact', JSON.stringify([...n]))
        return n
      })
    },
    toggleVisible: (code) => {
      setHidden((prev) => {
        const n = new Set(prev)
        n.has(code) ? n.delete(code) : n.add(code)
        localStorage.setItem('bach.hidden', JSON.stringify([...n]))
        return n
      })
    },
    toggleCompact: (code) => {
      setCompact((prev) => {
        const n = new Set(prev)
        n.has(code) ? n.delete(code) : n.add(code)
        localStorage.setItem('bach.compact', JSON.stringify([...n]))
        return n
      })
    },
    setCompactFor: (codes, value) => {
      setCompact((prev) => {
        const n = new Set(prev)
        codes.forEach((c) => (value ? n.add(c) : n.delete(c)))
        localStorage.setItem('bach.compact', JSON.stringify([...n]))
        return n
      })
    },
    importHeld: async () => {
      // 추가된 종목은 백엔드 status 브로드캐스트로 store에 반영됨. 개수만 반환.
      const r = await api.importHeld()
      return r.added || []
    },
    push: (code) => api.push(code),
    buy: (code, amount) => api.buy(code, amount),
    sell: (code, qty) => api.sell(code, qty),
    putConfig: async (code, config) => {
      await api.putConfig(code, config)
    },
    marketOpen: () => api.marketOpen(),
    marketClose: () => api.marketClose(),
    marketReset: () => api.marketReset(),
  }

  const value = { stocks, order, hidden, compact, ticks, phase, marketAuto, logs, connected, actions }
  return <StoreCtx.Provider value={value}>{children}</StoreCtx.Provider>
}
