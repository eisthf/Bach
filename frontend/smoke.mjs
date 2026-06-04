import { chromium } from 'playwright'

const errors = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } })
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', (e) => errors.push('PAGEERROR: ' + e.message))

await page.goto('http://localhost:5173/', { waitUntil: 'networkidle' })

// reset market to PRE_OPEN for a clean run
await page.evaluate(() => fetch('/api/market/reset', { method: 'POST' }))

// add a stock
await page.fill('.code-input', '000660')
await page.fill('.name-input', 'SK하이닉스')
await page.click('.add-btn')
await page.waitForSelector('.stock-panel', { timeout: 5000 })

// chart canvas present?
await page.waitForSelector('.stock-panel canvas', { timeout: 5000 })
const canvases = await page.locator('.stock-panel canvas').count()

// wait for at least one tick to update the price ticker
await page.waitForTimeout(2500)
const price = await page.locator('.ticker-price').first().innerText()

// state button shows current state
const stateName = await page.locator('.state-name').first().innerText()

// PUSH (pre-open): MANUAL -> MONITOR
await page.click('.state-btn')
await page.waitForTimeout(400)
const stateAfterPush = await page.locator('.state-name').first().innerText()

// market open: MONITOR -> AUTO_TRADING
await page.click('text=장 시작')
await page.waitForTimeout(2500)
const stateAfterOpen = await page.locator('.state-name').first().innerText()

// interval switch to 60 (scoped to first panel)
const panel = page.locator('.stock-panel').first()
await panel.locator('.iv-btn', { hasText: '60분' }).click()
await page.waitForTimeout(800)
const active60 = await panel.locator('.iv-btn.active').innerText()

// crosshair: hover over chart
const box = await page.locator('.stock-panel canvas').first().boundingBox()
await page.mouse.move(box.x + box.width * 0.6, box.y + box.height * 0.5)
await page.waitForTimeout(300)

// some logs present?
const logCount = await page.locator('.log-row').count()

await page.screenshot({ path: 'smoke.png', fullPage: true })
await browser.close()

console.log(JSON.stringify({
  canvases, price, stateName, stateAfterPush, stateAfterOpen, active60, logCount, errors,
}, null, 2))

if (errors.length) { console.error('CONSOLE ERRORS PRESENT'); process.exit(1) }
