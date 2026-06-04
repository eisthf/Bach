// 백엔드 REST 래퍼. Vite proxy 통해 localhost:8000 으로 전달.

async function req(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  if (!res.ok) {
    const txt = await res.text().catch(() => '')
    throw new Error(`${res.status} ${txt}`)
  }
  return res.status === 204 ? null : res.json()
}

export const api = {
  listStocks: () => req('/api/stocks'),
  addStock: (code, name) =>
    req('/api/stocks', { method: 'POST', body: JSON.stringify({ code, name }) }),
  removeStock: (code) => req(`/api/stocks/${code}`, { method: 'DELETE' }),

  getBars: (code, interval, lookbackExtra = 60) =>
    req(`/api/bars?code=${code}&interval=${interval}&lookback_extra=${lookbackExtra}`),

  buy: (code, amount_krw) =>
    req('/api/orders/buy', { method: 'POST', body: JSON.stringify({ code, amount_krw }) }),
  sell: (code, qty) =>
    req('/api/orders/sell', { method: 'POST', body: JSON.stringify({ code, qty }) }),
  positions: () => req('/api/positions'),

  getConfig: (code) => req(`/api/config/${code}`),
  putConfig: (code, config) =>
    req(`/api/config/${code}`, { method: 'PUT', body: JSON.stringify(config) }),

  push: (code) => req(`/api/state/${code}/push`, { method: 'POST' }),
  liquidate: (code) => req(`/api/state/${code}/liquidate`, { method: 'POST' }),

  market: () => req('/api/market'),
  marketOpen: () => req('/api/market/open', { method: 'POST' }),
  marketClose: () => req('/api/market/close', { method: 'POST' }),
  marketReset: () => req('/api/market/reset', { method: 'POST' }),
}
