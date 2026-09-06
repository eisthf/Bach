import assert from 'node:assert/strict'
import { chromium } from 'playwright'

// 저장된 AUTO_TRADING 종목으로 재시작한 합성 데모를 대상으로 실행한다.
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } })
const errors = []
page.on('pageerror', (e) => errors.push(e.message))
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
try {
  await page.addInitScript(() => {
    localStorage.setItem('bach.hidden.mock', JSON.stringify(['000660']))
    localStorage.setItem('bach.compact.mock', JSON.stringify(['000660']))
  })
  await page.goto(process.env.BACH_SMOKE_URL || 'http://localhost:5173/', { waitUntil: 'networkidle' })
  const panel = page.locator('.stock-panel').filter({ hasText: '000660' })
  await panel.locator('.recovery-notice').waitFor()
  assert.match(await panel.locator('.recovery-notice').innerText(), /AUTO_TRADING.*자동매매 보호 없음/)
  assert.equal(await panel.locator('.state-name').innerText(), '수동매매')
  assert.match(await panel.getAttribute('class'), /compact/)
  await page.locator('.log-row').filter({ hasText: '재시작 수동 인계' }).first().waitFor()
  await page.reload({ waitUntil: 'networkidle' })
  await panel.locator('.recovery-notice').waitFor()
  await page.screenshot({ path: process.env.BACH_SMOKE_SCREENSHOT || '/tmp/bach-restore.png', fullPage: true })
  assert.deepEqual(errors, [])
  console.log('복원 수동 상태·인계 로그·숨김/컴팩트·새로고침 표시 검증 통과')
} finally {
  await browser.close()
}
