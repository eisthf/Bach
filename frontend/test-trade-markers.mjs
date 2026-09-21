import assert from 'node:assert/strict'
import { tradeMarkers, tradePoints } from './src/tradeMarkers.js'

const start = Date.UTC(2026, 8, 21, 9) / 1000
const bars = [0, 180, 540].map(offset => ({ time: start + offset }))
const trade = (offset, side, qty, price) => ({ time: start + offset, side, qty, price })
const markers = tradeMarkers(bars, [
  trade(1, 'buy', 2, 100), trade(179, 'buy', 3, 110),
  trade(100, 'sell', 1, 120), trade(180, 'sell', 2, 125),
  trade(-1, 'buy', 1, 90), trade(400, 'buy', 1, 90),
  trade(720, 'buy', 1, 90), trade(10, 'buy', 0, 90),
], 3)
assert.equal(markers.length, 3)
assert.equal(markers[0].text, 'B 5주 · 106원')
assert.equal(markers[0].position, 'belowBar')
assert.equal(markers[1].shape, 'arrowDown')
assert.equal(markers[2].time, start + 180)
const points = tradePoints(bars, [trade(1, 'buy', 2, 100), trade(179, 'buy', 3, 110), trade(100, 'sell', 1, 120), trade(400, 'buy', 1, 90)], 3)
assert.deepEqual(points.map(p => [p.time, p.side, p.price]), [[start, 'buy', 100], [start, 'buy', 110], [start, 'sell', 120]])
const midnight = Date.UTC(2026, 8, 21) / 1000
assert.equal(tradeMarkers([{ time: midnight }], [trade(20, 'buy', 1, 100)], 1440)[0].time, midnight)
console.log('PASS: fill aggregation, buy/sell separation, minute/day alignment, missing-bar exclusion, per-fill cross points')
