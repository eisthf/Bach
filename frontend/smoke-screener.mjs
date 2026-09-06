// 상한가 종목 페이지 스모크 — 페이지 전환 + 표 렌더 + 날짜 조회.
//   node smoke-screener.mjs
// 환경: BACH_SMOKE_URL(기본 http://localhost:5173/)
import { chromium } from 'playwright'

const errors = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } })
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', (e) => errors.push('PAGEERROR: ' + e.message))

const base = process.env.BACH_SMOKE_URL || 'http://localhost:5173/'
await page.goto(base, { waitUntil: 'networkidle' })

// 매매 페이지가 기본 라우트인가
const tradingVisible = await page.locator('.main-grid').isVisible()

// 상한가 종목 페이지로 전환
await page.click('.page-tab:has-text("상한가 종목")')
await page.waitForSelector('.screener', { timeout: 5000 })
await page.waitForSelector('.data-table, .screener-empty, .screener-error', { timeout: 15000 })

const hash = await page.evaluate(() => location.hash)
const summary = await page.locator('.screener-summary').innerText().catch(() => '(요약 없음)')
const rows = await page.locator('.data-table tbody tr').count()
const headers = await page.locator('.data-table thead th').allInnerTexts().catch(() => [])
const firstRow = rows
  ? await page.locator('.data-table tbody tr').first().allInnerTexts()
  : []

// 조회된 날짜가 입력칸에 반영됐는가 → 그 날짜로 다시 조회해도 같은 결과
const dateValue = await page.inputValue('#ul-date')
await page.click('.screener-controls button.primary')
await page.waitForTimeout(1200)
const rowsAfterRequery = await page.locator('.data-table tbody tr').count()

// 새로고침해도 상한가 페이지가 유지되는가(해시 라우팅)
await page.reload({ waitUntil: 'networkidle' })
await page.waitForSelector('.screener', { timeout: 5000 })
const keptAfterReload = await page.locator('.page-tab.active').innerText()
await page.waitForSelector('.data-table, .screener-empty, .screener-error', { timeout: 15000 })
await page.screenshot({ path: process.env.BACH_SMOKE_SHOT || 'screener.png', fullPage: true })

// 매매 페이지로 복귀
await page.click('.page-tab:has-text("매매")')
await page.waitForSelector('.main-grid', { timeout: 5000 })
const backToTrading = await page.locator('.main-grid').isVisible()

console.log(JSON.stringify({
  tradingVisible,
  hash,
  dateValue,
  headers,
  rows,
  rowsAfterRequery,
  firstRow,
  keptAfterReload,
  backToTrading,
  summary: summary.replace(/\n/g, ' | '),
  errors,
}, null, 2))

await browser.close()

if (errors.length) {
  console.error('콘솔 오류 발생')
  process.exit(1)
}
