// 전역 상태: 계좌 목록 + 계좌별 종목/상태/실시간 시세/미체결/로그 + 장 단계.
// WebSocket 메시지의 account 필드로 계좌별 슬라이스에 라우팅한다.
import React, { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api'

const StoreCtx = createContext(null)
export const useStore = () => useContext(StoreCtx)

// 계좌별 맵에서 한 계좌 슬롯을 안전하게 갱신하는 헬퍼.
const setIn = (setter, acc, updater) =>
  setter((prev) => ({ ...prev, [acc]: updater(prev[acc]) }))

const loadSet = (key) => {
  try { return new Set(JSON.parse(localStorage.getItem(key) || '[]')) }
  catch { return new Set() }
}

export function StoreProvider({ children }) {
  const [accounts, setAccounts] = useState([])     // [{id,label,live,danger}]
  const [stocks, setStocks] = useState({})         // acc -> {code: status}
  const [order, setOrder] = useState({})           // acc -> [codes]
  const [ticks, setTicks] = useState({})           // acc -> {code: tick}
  const [orders, setOrders] = useState({})         // acc -> {code: [orders]}
  const [hidden, setHidden] = useState({})         // acc -> Set(codes)
  const [compact, setCompact] = useState({})       // acc -> Set(codes)
  const [phase, setPhase] = useState('PRE_OPEN')
  const [marketAuto, setMarketAuto] = useState(false)
  const [logs, setLogs] = useState([])             // [{t, text, account}]
  const [connected, setConnected] = useState(false)
  const wsRef = useRef(null)
  // 액션의 confirm 가드가 최신 danger 정보를 보도록 ref로 보관.
  const dangerRef = useRef({})

  useEffect(() => {
    const d = {}
    accounts.forEach((a) => (d[a.id] = !!a.danger))
    dangerRef.current = d
  }, [accounts])

  // 초기 스냅샷: 계좌 목록 → 계좌별 종목 + 클라이언트 표시상태(localStorage).
  useEffect(() => {
    api.accounts().then((list) => {
      setAccounts(list)
      const h = {}, c = {}
      list.forEach((a) => {
        h[a.id] = loadSet(`bach.hidden.${a.id}`)
        c[a.id] = loadSet(`bach.compact.${a.id}`)
      })
      setHidden(h)
      setCompact(c)
      list.forEach((a) => {
        api.listStocks(a.id).then((rows) => {
          const map = {}
          rows.forEach((s) => (map[s.code] = s))
          setIn(setStocks, a.id, () => map)
          setIn(setOrder, a.id, () => rows.map((s) => s.code))
        })
      })
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
        const acc = msg.account
        if (msg.type === 'accounts') {
          setAccounts(msg.accounts || [])
        } else if (msg.type === 'tick') {
          const t = msg.tick
          setIn(setTicks, acc, (m) => ({ ...(m || {}), [t.code]: t }))
        } else if (msg.type === 'status') {
          const s = msg.status
          setIn(setStocks, acc, (m) => ({ ...(m || {}), [s.code]: s }))
          setIn(setOrder, acc, (arr) =>
            (arr || []).includes(s.code) ? arr : [...(arr || []), s.code])
        } else if (msg.type === 'orders') {
          setIn(setOrders, acc, () => msg.orders || {})
        } else if (msg.type === 'market') {
          setPhase(msg.phase)
          if (msg.auto !== undefined) setMarketAuto(!!msg.auto)
        } else if (msg.type === 'log') {
          setLogs((prev) => [...prev.slice(-300), { t: Date.now(), text: msg.text, account: acc }])
        }
      }
    }
    connect()
    return () => {
      stopped = true
      wsRef.current?.close()
    }
  }, [])

  // 표시상태 토글 헬퍼(localStorage 동기화)
  const toggleSet = (setter, key, acc, code) =>
    setIn(setter, acc, (s) => {
      const n = new Set(s || [])
      n.has(code) ? n.delete(code) : n.add(code)
      localStorage.setItem(key(acc), JSON.stringify([...n]))
      return n
    })

  const actions = useMemo(() => ({
    addStock: async (acc, code, name) => {
      const s = await api.addStock(acc, code, name)
      setIn(setStocks, acc, (m) => ({ ...(m || {}), [s.code]: s }))
      setIn(setOrder, acc, (arr) =>
        (arr || []).includes(s.code) ? arr : [...(arr || []), s.code])
    },
    removeStock: async (acc, code) => {
      await api.removeStock(acc, code)
      setIn(setStocks, acc, (m) => {
        const n = { ...(m || {}) }
        delete n[code]
        return n
      })
      setIn(setOrder, acc, (arr) => (arr || []).filter((c) => c !== code))
    },
    toggleVisible: (acc, code) =>
      toggleSet(setHidden, (a) => `bach.hidden.${a}`, acc, code),
    toggleCompact: (acc, code) =>
      toggleSet(setCompact, (a) => `bach.compact.${a}`, acc, code),
    setCompactFor: (acc, codes, value) =>
      setIn(setCompact, acc, (s) => {
        const n = new Set(s || [])
        codes.forEach((c) => (value ? n.add(c) : n.delete(c)))
        localStorage.setItem(`bach.compact.${acc}`, JSON.stringify([...n]))
        return n
      }),
    importHeld: async (acc) => {
      const r = await api.importHeld(acc)
      return r.added || []
    },
    push: (acc, code) => api.push(acc, code),
    buy: (acc, code, amount) => {
      if (dangerRef.current[acc] &&
          !window.confirm(`⚠️ 실전 계좌 매수\n${code} ${Number(amount).toLocaleString()}원\n실제 주문이 나갑니다. 진행할까요?`)) {
        return Promise.resolve({ ok: false, message: '취소됨(실전 가드)' })
      }
      return api.buy(acc, code, amount)
    },
    sell: (acc, code, qty) => {
      if (dangerRef.current[acc] &&
          !window.confirm(`⚠️ 실전 계좌 매도\n${code} ${Number(qty).toLocaleString()}주\n실제 주문이 나갑니다. 진행할까요?`)) {
        return Promise.resolve({ ok: false, message: '취소됨(실전 가드)' })
      }
      return api.sell(acc, code, qty)
    },
    putConfig: async (acc, code, config) => {
      await api.putConfig(acc, code, config)
    },
    marketOpen: () => api.marketOpen(),
    marketClose: () => api.marketClose(),
    marketReset: () => api.marketReset(),
  }), [])

  const value = {
    accounts, stocks, order, ticks, orders, hidden, compact,
    phase, marketAuto, logs, connected, actions,
  }
  return <StoreCtx.Provider value={value}>{children}</StoreCtx.Provider>
}
