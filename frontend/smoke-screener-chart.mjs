import assert from 'node:assert/strict'
import { chromium } from 'playwright'

const browser = await chromium.launch(process.env.BACH_BROWSER_CHANNEL ? { channel: process.env.BACH_BROWSER_CHANNEL } : {})
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  const errors = []
  const intervals = []
  let failBars = false
  page.on('pageerror', (error) => errors.push(error.message))
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    let data = {}
    if (url.pathname === '/api/accounts') data = [{ id: 'mock', label: '데모', live: false }]
    else if (url.pathname.endsWith('/upper-limit')) data = {
      date: '2026-09-18', prev_date: '2026-09-17', source: 'mock', scanned: 1,
      stocks: [{ code: '005930', name: '테스트 종목', market: 'KOSPI', market_cap: 1000000000, volume: 10000, close: 13000, prev_close: 10000, change_pct: 30 }],
    }
    else if (url.pathname.endsWith('/bars')) {
      assert.equal(route.request().method(), 'GET')
      const interval = Number(url.searchParams.get('interval'))
      intervals.push(interval)
      if (interval === 3) assert.equal(url.searchParams.get('session_only'), 'true')
      if (failBars) return route.fulfill({ status: 503, json: { detail: '시세 조회 실패' } })
      data = { day_start_index: 60, bars: Array.from({ length: 180 }, (_, i) => ({ time: 1700000000 + i * interval * 60, open: 10000 + i, high: 10010 + i, low: 9990 + i, close: 10005 + i, volume: 1000 + i })) }
    }
    else if (url.pathname.endsWith('/stocks')) data = []
    await route.fulfill({ json: data })
  })
  await page.goto(process.env.BACH_SMOKE_URL || 'http://127.0.0.1:5174/#/upper-limit')
  await page.getByRole('button', { name: '테스트 종목 차트 보기' }).click()
  await page.waitForFunction(() => !document.querySelector('dialog [role="status"]'))
  assert.equal(await page.locator('dialog canvas').count() > 0, true)
  await page.getByRole('button', { name: '일봉', exact: true }).click()
  await page.waitForFunction(() => !document.querySelector('dialog [role="status"]'))
  assert.deepEqual([...new Set(intervals)], [3, 1440])
  assert.match(await page.locator('dialog').innerText(), /MA5.*MA10.*MA20.*MA60/s)
  await page.keyboard.press('Escape')
  assert.equal(await page.locator('dialog').count(), 0)
  await page.setViewportSize({ width: 390, height: 844 })
  failBars = true
  await page.locator('.screener-stock-row').click()
  await page.getByRole('alert').waitFor()
  failBars = false
  await page.getByRole('button', { name: '다시 시도' }).click()
  await page.waitForFunction(() => !document.querySelector('dialog [role="status"], dialog [role="alert"]'))
  const bounds = await page.locator('dialog').boundingBox()
  assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 390)
  await page.getByRole('button', { name: '차트 닫기' }).click()
  assert.equal(await page.locator('dialog').count(), 0)
  assert.deepEqual(errors, [])
  console.log('PASS: chart open, 3-minute/daily bars, MA legend, retry, mobile layout, close')
} finally {
  await browser.close()
}
