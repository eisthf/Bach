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
  const [candleTrades, setCandleTrades] = useState({}) // acc -> {code: 대금 양봉 매매 항목}
  const [candlePositions, setCandlePositions] = useState({}) // acc -> {code: 계좌 포지션}
  const [manualTrades, setManualTrades] = useState({}) // acc -> {code: 수동 매매 항목(포지션 포함)}
  const [hidden, setHidden] = useState({})         // acc -> Set(codes)
  const [compact, setCompact] = useState({})       // acc -> Set(codes)
  const [phase, setPhase] = useState('PRE_OPEN')
  const [marketAuto, setMarketAuto] = useState(false)
  const [logs, setLogs] = useState([])             // [{t, text, account}]
  const [connected, setConnected] = useState(false)
  // 표시할 계좌 필터: 'ALL' | 계좌 id. 새로고침해도 유지(localStorage).
  const [accountView, setAccountViewRaw] = useState(
    () => localStorage.getItem('bach.accountView') || 'ALL')
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
      // 늦게 도착한 HTTP 스냅샷이 WS에서 받은 최신 연결 상태를 덮지 않게 한다.
      setAccounts((prev) => list.map((a) => ({ ...a,
        connection: prev.find((p) => p.id === a.id)?.connection || a.connection,
      })))
      const h = {}, c = {}
      list.forEach((a) => {
        h[a.id] = loadSet(`bach.hidden.${a.id}`)
        c[a.id] = loadSet(`bach.compact.${a.id}`)
      })
      setHidden(h)
      setCompact(c)
      list.forEach((a) => {
        api.manualTrades(a.id).then((rows) => {
          const map = {}
          rows.forEach((it) => (map[it.code] = it))
          setIn(setManualTrades, a.id, () => map)
        }).catch(() => {})
        api.candleTrades(a.id).then((rows) => {
          const map = {}
          rows.forEach((it) => (map[it.code] = it))
          setIn(setCandleTrades, a.id, () => map)
        }).catch(() => {})
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
      ws.onopen = () => { if (!stopped) setConnected(true) }
      ws.onclose = () => {
        if (stopped) return
        setConnected(false)
        setTimeout(() => { if (!stopped) connect() }, 1000)
      }
      ws.onmessage = (ev) => {
        if (stopped) return
        const msg = JSON.parse(ev.data)
        const acc = msg.account
        if (msg.type === 'accounts') {
          setAccounts(msg.accounts || [])
        } else if (msg.type === 'broker_connection') {
          setAccounts((prev) => prev.map((a) => a.id === acc ? { ...a, connection: msg.status } : a))
        } else if (msg.type === 'tick') {
          const t = msg.tick
          setIn(setTicks, acc, (m) => ({ ...(m || {}), [t.code]: t }))
        } else if (msg.type === 'status') {
          const s = msg.status
          setIn(setStocks, acc, (m) => ({ ...(m || {}), [s.code]: s }))
          setIn(setOrder, acc, (arr) =>
            (arr || []).includes(s.code) ? arr : [...(arr || []), s.code])
        } else if (msg.type === 'stock_removed') {
          // [수동 전환]으로 [상한가 매매] 목록에서 빠졌다(수동 매매로 옮겨감).
          setIn(setStocks, acc, (m) => {
            const n = { ...(m || {}) }
            delete n[msg.code]
            return n
          })
          setIn(setOrder, acc, (arr) => (arr || []).filter((c) => c !== msg.code))
        } else if (msg.type === 'manual_trade') {
          const it = msg.item
          setIn(setManualTrades, acc, (m) => ({ ...(m || {}), [it.code]: it }))
        } else if (msg.type === 'manual_trade_removed') {
          setIn(setManualTrades, acc, (m) => {
            const n = { ...(m || {}) }
            delete n[msg.code]
            return n
          })
        } else if (msg.type === 'candle_trade') {
          const it = msg.item
          setIn(setCandleTrades, acc, (m) => ({ ...(m || {}), [it.code]: it }))
        } else if (msg.type === 'candle_trade_removed') {
          setIn(setCandleTrades, acc, (m) => {
            const n = { ...(m || {}) }
            delete n[msg.code]
            return n
          })
        } else if (msg.type === 'candle_trade_position') {
          setIn(setCandlePositions, acc, (m) => ({ ...(m || {}), [msg.code]: msg.position }))
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
    // 길게 누르기: codes(칩바의 종목) 중 code만 보이고 나머지는 숨긴다. 이미 그 종목만
    // 보이는 중이면 전부 다시 보인다.
    soloVisible: (acc, code, codes) =>
      setIn(setHidden, acc, (s) => {
        const prev = s || new Set()
        const others = codes.filter((c) => c !== code)
        const already = !prev.has(code) && others.every((c) => prev.has(c))
        const n = new Set(prev)
        if (already) others.forEach((c) => n.delete(c))
        else { n.delete(code); others.forEach((c) => n.add(c)) }
        localStorage.setItem(`bach.hidden.${acc}`, JSON.stringify([...n]))
        return n
      }),
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
    setAccountView: (v) => {
      localStorage.setItem('bach.accountView', v)
      setAccountViewRaw(v)
    },
    push: (acc, code) => api.push(acc, code),
    // 수동 전환은 되돌릴 수 없다(다시 자동매매하려면 보유 0에서 수동 매매에서 지우고 새로 추가).
    transferToManual: (acc, code, what = '상한가 자동매매') => {
      if (!window.confirm(`${code} ${what}를 멈추고 수동 매매로 옮길까요?
이후 자동 주문은 나가지 않으며, 되돌릴 수 없습니다.`)) {
        return Promise.resolve(null)
      }
      return api.transferToManual(acc, code)
    },
    addManual: (acc, code, name) => api.addManualTrade(acc, code, name),
    removeManual: (acc, code) => api.removeManualTrade(acc, code),
    setManualExits: (acc, code, stop, takeProfit) => api.setManualExits(acc, code, stop, takeProfit),
    clearManualNotice: (acc, code) => api.clearManualNotice(acc, code),
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
    // 대금 양봉 매매. 실전 계좌의 감시 시작(이후 자동 주문)은 확인을 한 번 더 받는다.
    setCandleConfig: (acc, code, config) => api.setCandleConfig(acc, code, config),
    setCandleZone: (acc, code, low, high) => api.setCandleZone(acc, code, low, high),
    addZoneTrade: (acc, code, name) => api.addZoneTrade(acc, code, name),
    // 새 구간은 대기 차수를 되돌릴 수 없이 취소하므로 계좌 종류와 상관없이 확인을 받는다.
    setRoundBudget: (acc, code, budget) => api.setRoundBudget(acc, code, budget),
    rezoneCandle: (acc, code, low, high, budget, message) => {
      if (!window.confirm(message)) return Promise.resolve(null)
      return api.rezoneCandle(acc, code, low, high, budget)
    },
    startCandle: (acc, code, message) => {
      if (dangerRef.current[acc] && !window.confirm(message)) return Promise.resolve(null)
      return api.startCandle(acc, code)
    },
    stopCandle: (acc, code) => api.stopCandle(acc, code),
    handoffCandle: (acc, code) => {
      if (!window.confirm(`${code} 자동 매매를 멈추고 수동 매매로 옮길까요?
이후 자동 매수·추가매수·익절·트레일링·손절은 하지 않으며, 되돌릴 수 없습니다.`)) {
        return Promise.resolve(null)
      }
      return api.handoffCandle(acc, code)
    },
    removeCandle: (acc, code) => api.removeCandle(acc, code),
    marketOpen: () => api.marketOpen(),
    marketClose: () => api.marketClose(),
    marketReset: () => api.marketReset(),
  }), [])

  // 저장된 계좌가 사라졌거나(ACCOUNTS 변경) 계좌가 하나뿐이면 전체 보기로 폴백.
  const viewAcc =
    accounts.length > 1 && accounts.some((a) => a.id === accountView) ? accountView : 'ALL'

  const value = {
    accounts, stocks, order, ticks, orders, hidden, compact, candleTrades, candlePositions, manualTrades,
    phase, marketAuto, logs, connected, accountView: viewAcc, actions,
  }
  return <StoreCtx.Provider value={value}>{children}</StoreCtx.Provider>
}
