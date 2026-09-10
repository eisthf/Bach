// 합성 서버 위에서 증권사 상태와 KRX 게시 대기 응답만 대체한다.
// BACH_SMOKE_URL / BACH_SMOKE_DIR (기본 /tmp)
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { chromium } from 'playwright'

const browser = await chromium.launch()
try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } })
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  const good = { auth: 'ok', rest: 'ok', stream: 'connected', sync: 'ok', last_received_at: '2026-09-10T09:01:23+09:00' }
  const account = { id: 'mock', label: '모의투자', live: true, danger: false, connection: good }
  await page.route('**/api/accounts', (route) => route.fulfill({ json: [account] }))
  let socket
  await page.routeWebSocket('**/ws**', (ws) => {
    socket = ws
    ws.send(JSON.stringify({ type: 'accounts', accounts: [account] }))
    ws.send(JSON.stringify({ type: 'market', phase: 'PRE_OPEN', auto: true }))
  })
  await page.goto(process.env.BACH_SMOKE_URL || 'http://127.0.0.1:15183', { waitUntil: 'networkidle' })
  if (process.env.BACH_SMOKE_FONT) {
    const font = fs.readFileSync(process.env.BACH_SMOKE_FONT).toString('base64')
    await page.addStyleTag({ content: `@font-face { font-family: SmokeKorean; src: url(data:font/ttf;base64,${font}); } body, button, input { font-family: SmokeKorean, sans-serif !important; }` })
    await page.evaluate(() => document.fonts.ready)
  }
  await page.locator('.broker-connection').waitFor()
  await page.waitForFunction(() => document.querySelector('.broker-connection')?.textContent.includes('인증 정상'))
  assert.match(await page.locator('.broker-connection').innerText(), /인증 정상/)
  socket.send(JSON.stringify({ type: 'broker_connection', account: 'mock', status: {
    ...good, auth: 'error', rest: 'error', stream: 'auth_error', sync: 'error',
  } }))
  await page.waitForFunction(() => document.querySelector('.broker-connection')?.textContent.includes('인증 실패'))
  assert.match(await page.locator('.conn').getAttribute('aria-label'), /앱 서버 연결됨/)
  assert.equal(await page.locator('.broker-connection.warning').count(), 1)
  const dir = process.env.BACH_SMOKE_DIR || '/tmp'
  await page.screenshot({ path: `${dir}/bach-broker-connection.png` })
  socket.send(JSON.stringify({ type: 'broker_connection', account: 'mock', status: good }))
  await page.waitForFunction(() => document.querySelector('.broker-connection')?.textContent.includes('인증 정상'))
  assert.equal(await page.locator('.broker-connection.warning').count(), 0)

  let published = false
  await page.route('**/api/screener/upper-limit**', (route) => published
    ? route.fulfill({ json: { date: '2026-09-09', prev_date: '2026-09-08', source: 'krx', snapshot: false, scanned: 2765, stocks: [], notice: '' } })
    : route.fulfill({ status: 409, json: { detail: { code: 'DATA_PENDING', message: '2026-09-09 KRX 종가 자료가 아직 게시되지 않았습니다. 영업일 기준 다음 날 오전 8시에 갱신됩니다.' } } }))
  await page.getByRole('button', { name: '상한가 종목', exact: true }).click()
  await page.locator('.screener-notice').waitFor()
  assert.match(await page.locator('.screener-notice').innerText(), /KRX 자료 게시 대기/)
  assert.equal(await page.locator('.screener-error').count(), 0)
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
  await page.screenshot({ path: `${dir}/bach-krx-pending.png`, fullPage: true })
  published = true
  await page.getByRole('button', { name: '다시 조회', exact: true }).click()
  await page.locator('.screener-summary').waitFor()
  assert.match(await page.locator('.screener-summary').innerText(), /2026-09-09/)
  assert.equal(await page.locator('.screener-notice').count(), 0)
  assert.deepEqual(errors, [])
  console.log('PASS: app/broker connection distinction, recovery, KRX pending -> published, mobile overflow')
} finally {
  await browser.close()
}
