// 백엔드 REST 래퍼. Vite proxy 통해 localhost:8000 으로 전달.
// 계좌 스코프 엔드포인트는 /api/{account}/... 로 호출한다.

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

const enc = encodeURIComponent

export const api = {
  // 전역
  accounts: () => req('/api/accounts'),
  market: () => req('/api/market'),
  marketOpen: () => req('/api/market/open', { method: 'POST' }),
  marketClose: () => req('/api/market/close', { method: 'POST' }),
  marketReset: () => req('/api/market/reset', { method: 'POST' }),

  // 계좌 스코프 (a = account id)
  listStocks: (a) => req(`/api/${enc(a)}/stocks`),
  addStock: (a, code, name) =>
    req(`/api/${enc(a)}/stocks`, { method: 'POST', body: JSON.stringify({ code, name }) }),
  removeStock: (a, code) => req(`/api/${enc(a)}/stocks/${code}`, { method: 'DELETE' }),
  importHeld: (a) => req(`/api/${enc(a)}/stocks/import-held`, { method: 'POST' }),

  // 일봉은 최근 60일 전체에 MA60을 그릴 수 있도록 59일의 계산 여유분을 더 받는다.
  getBars: (a, code, interval, lookbackExtra = interval === 1440 ? 119 : 60) =>
    req(`/api/${enc(a)}/bars?code=${code}&interval=${interval}&lookback_extra=${lookbackExtra}`),

  buy: (a, code, amount_krw) =>
    req(`/api/${enc(a)}/orders/buy`, { method: 'POST', body: JSON.stringify({ code, amount_krw }) }),
  sell: (a, code, qty) =>
    req(`/api/${enc(a)}/orders/sell`, { method: 'POST', body: JSON.stringify({ code, qty }) }),
  positions: (a) => req(`/api/${enc(a)}/positions`),
  account: (a) => req(`/api/${enc(a)}/account`),

  getConfig: (a, code) => req(`/api/${enc(a)}/config/${code}`),
  putConfig: (a, code, config) =>
    req(`/api/${enc(a)}/config/${code}`, { method: 'PUT', body: JSON.stringify(config) }),

  push: (a, code) => req(`/api/${enc(a)}/state/${code}/push`, { method: 'POST' }),
}
