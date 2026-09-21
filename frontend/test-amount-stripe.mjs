import assert from 'node:assert/strict'
import { AMOUNT_STRIPE_MIN, amountStripeTimes } from './src/amountStripePrimitive.js'

assert.equal(AMOUNT_STRIPE_MIN, 15_000_000_000)
const bars = [
  { time: 1, amount: 14_999_999_999 }, { time: 2, amount: 15_000_000_000 },
  { time: 3, amount: 102_000_000_000 }, { time: 4 }, { time: 5, amount: 0 },
]
assert.deepEqual(amountStripeTimes(bars), [2, 3])
console.log('PASS: 150억 이상(경계 포함) 봉만 스트라이프, 거래대금 없는 봉 제외')
