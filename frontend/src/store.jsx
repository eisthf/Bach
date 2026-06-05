// 전역 상태: 종목 목록/상태/실시간 시세/로그/장 단계 + WebSocket 구독.
import React, { createContext, useContext, useEffect, useRef, useState } from 'react'
import { api } from './api'

const StoreCtx = createContext(null)
export const useStore = () => useContext(StoreCtx)

export function StoreProvider({ children }) {
  // code -> StockStatus({code,name,state,config,position})
  const [stocks, setStocks] = useState({})
  // code -> latest tick({code,price,high,low,open,...})
  const [ticks, setTicks] = useState({})
  const [phase, setPhase] = useState('PRE_OPEN')
  const [marketAuto, setMarketAuto] = useState(false)
  const [logs, setLogs] = useState([])
  const [connected, setConnected] = useState(false)
  const wsRef = useRef(null)

  // 초기 스냅샷
  useEffect(() => {
    api.listStocks().then((list) => {
      const map = {}
      list.forEach((s) => (map[s.code] = s))
      setStocks(map)
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
    },
    removeStock: async (code) => {
      await api.removeStock(code)
      setStocks((prev) => {
        const n = { ...prev }
        delete n[code]
        return n
      })
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

  const value = { stocks, ticks, phase, marketAuto, logs, connected, actions }
  return <StoreCtx.Provider value={value}>{children}</StoreCtx.Provider>
}
