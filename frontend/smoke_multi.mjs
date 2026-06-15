import { chromium } from 'playwright'

const BASE = process.env.BASE || 'http://localhost:5174/'
const errors = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1500, height: 950 } })
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', (e) => errors.push('PAGEERROR: ' + e.message))

await page.goto(BASE, { waitUntil: 'networkidle' })
await page.waitForSelector('.account-col', { timeout: 8000 })

const cols = await page.locator('.account-col').count()
const badges = await page.locator('.account-badge').allInnerTexts()
const dangerCols = await page.locator('.account-col.danger').count()

// 실전 컬럼에 종목 추가 → 패널/차트 렌더 확인 (real = 합성 데모 데이터)
const realCol = page.locator('.account-col.danger')
await realCol.locator('.code-input').fill('000660')
await realCol.locator('.name-input').fill('SK하이닉스')
await realCol.locator('.add-btn').click()
await realCol.locator('.stock-panel').first().waitFor({ timeout: 6000 })
await realCol.locator('.stock-panel canvas').first().waitFor({ timeout: 6000 })
await page.waitForTimeout(2000)
const realPanels = await realCol.locator('.stock-panel').count()
const realCanvas = await realCol.locator('.stock-panel canvas').count()
const realPrice = await realCol.locator('.ticker-price').first().innerText().catch(() => '-')

// 모의 컬럼 종목 수(복원분)
const mockCol = page.locator('.account-col.safe')
const mockPanels = await mockCol.locator('.stock-panel').count()

await page.screenshot({ path: 'smoke_multi.png', fullPage: true })
await browser.close()

console.log(JSON.stringify({
  cols, badges, dangerCols, realPanels, realCanvas, realPrice, mockPanels, errors,
}, null, 2))
if (errors.length) { console.error('CONSOLE ERRORS PRESENT'); process.exit(1) }
if (cols < 2 || dangerCols < 1) { console.error('LAYOUT ASSERTION FAILED'); process.exit(2) }
