import assert from 'node:assert/strict'
import { chromium } from 'playwright'

const errors = []
const browser = await chromium.launch()
const viewportWidth = Number(process.env.BACH_SMOKE_WIDTH || 390)
const page = await browser.newPage({
  viewport: { width: viewportWidth, height: 844 },
  isMobile: true,
  hasTouch: true,
})
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', (e) => errors.push(e.message))

try {
  await page.goto(process.env.BACH_SMOKE_URL || 'http://localhost:5173/', {
    waitUntil: 'networkidle',
  })
  await page.evaluate(() => fetch('/api/market/reset', { method: 'POST' }))
  await page.fill('.code-input', '000660')
  await page.fill('.name-input', 'SK하이닉스')
  await page.click('.add-btn')
  await page.waitForSelector('.stock-panel canvas')
  await page.locator('.stock-panel').first().locator('.iv-btn', { hasText: '일봉' }).click()
  await page.waitForTimeout(500)
  assert.equal(
    await page.locator('.stock-panel').first().locator('.iv-btn.active').innerText(),
    '일봉',
  )

  const metrics = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
    inputHeight: document.querySelector('.code-input').getBoundingClientRect().height,
    buttonHeight: document.querySelector('.add-btn').getBoundingClientRect().height,
    chartHeight: document.querySelector('.chart-canvas').getBoundingClientRect().height,
    accountWidth: document.querySelector('.account-col').getBoundingClientRect().width,
  }))
  assert.equal(metrics.scrollWidth, metrics.viewport, '페이지에 가로 스크롤이 생김')
  assert.ok(metrics.inputHeight >= 44)
  assert.ok(metrics.buttonHeight >= 44)
  assert.ok(metrics.chartHeight <= 280)
  assert.ok(metrics.accountWidth <= metrics.viewport)
  assert.deepEqual(errors, [])

  await page.screenshot({
    path: process.env.BACH_SMOKE_SCREENSHOT || 'smoke-mobile.png',
    fullPage: true,
  })
  console.log(JSON.stringify(metrics, null, 2))
} finally {
  await browser.close()
}
