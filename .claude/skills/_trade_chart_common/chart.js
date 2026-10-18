// 매매 차트 공통 JS — ulc-trade-chart·candle-trade-chart 가 write_html 로 주입한다.
// ✕ 표시·이평선·캔들 색은 Bach 프런트와 같아야 한다(바뀌면 여기만 고친다):
//   frontend/src/tradeCrossPrimitive.js, frontend/src/tradeMarkers.js, frontend/src/indicators.js

// -- 포맷 ---------------------------------------------------------------------
const fmt = n => n == null ? "—" : Math.round(n).toLocaleString("ko-KR");
const signed = n => n == null ? "—" : (n >= 0 ? "+" : "") + fmt(n);
const pct = n => (n > 0 ? "+" : "") + n.toFixed(2) + "%";
const cls = n => n == null ? "na" : n >= 0 ? "pos" : "neg";
const dayStr = d => `${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6)}`;
// 봉·체결 시각은 KST 벽시계를 UTC로 둔 epoch 라 ISO 문자열이 곧 KST 시각이다.
const iso = t => new Date(t * 1000).toISOString();
const ymdOf = t => iso(t).slice(0, 10).replace(/-/g, "");

// -- Bach 차트 규칙 -----------------------------------------------------------
const TRADE_COLORS = { buy: "#d32f2f", sell: "#1565c0" };
const ANCHOR_COLOR = "#7b1fa2";  // 기준봉
const MA_LINES = [
  { period: 5, color: "#2962FF", title: "MA5" }, { period: 10, color: "#FF4FA3", title: "MA10" },
  { period: 20, color: "#FF9800", title: "MA20" }, { period: 60, color: "#26A65B", title: "MA60" },
];
function sma(bars, period) {
  const out = []; let sum = 0;
  for (let i = 0; i < bars.length; i++) {
    sum += bars[i].close;
    if (i >= period) sum -= bars[i - period].close;
    if (i >= period - 1) out.push({ time: bars[i].time, value: sum / period });
  }
  return out;
}
// 체결 건마다 실제 체결가 위치에 ✕ (v4 마커는 봉 위/아래에만 붙고 X 모양이 없어 직접 그린다)
class TradeCross {
  constructor(points) {
    this.points = points;
    const src = this;
    this._views = [{ zOrder: () => "top", renderer: () => ({ draw(target) {
      const { chart, series, points } = src;
      if (!chart || !series) return;
      const ts = chart.timeScale();
      const half = Math.max(3, Math.min(6, ts.options().barSpacing * 0.3));
      target.useBitmapCoordinateSpace(({ context: ctx, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
        ctx.save(); ctx.lineCap = "round";
        for (const p of points) {
          const x = ts.timeToCoordinate(p.time), y = series.priceToCoordinate(p.price);
          if (x === null || y === null) continue;
          const cx = x * hr, cy = y * vr, dx = half * hr, dy = half * vr;
          ctx.beginPath();
          ctx.moveTo(cx - dx, cy - dy); ctx.lineTo(cx + dx, cy + dy);
          ctx.moveTo(cx + dx, cy - dy); ctx.lineTo(cx - dx, cy + dy);
          ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 4 * hr; ctx.stroke();
          ctx.strokeStyle = TRADE_COLORS[p.side]; ctx.lineWidth = 2 * hr; ctx.stroke();
        }
        ctx.restore();
      });
    } }) }];
  }
  attached({ chart, series, requestUpdate }) { this.chart = chart; this.series = series; requestUpdate(); }
  detached() { this.chart = this.series = null; }
  paneViews() { return this._views; }
}

// -- 차트 생성 ----------------------------------------------------------------
// 봉 시각 = ts 이하 마지막 봉(봉 시작 기준)
const barAt = (rows, ts) => { let best = rows[0].time; for (const b of rows) { if (b.time <= ts) best = b.time; else break; } return best; };
const charts = [];
// 패널이 처음에 폭 0으로 그려지는 환경이 있어, 크기가 바뀌면 폭과 표시 범위(fit)를 다시 맞춘다.
const chartResize = new ResizeObserver(entries => entries.forEach(e => {
  const hit = charts.find(c => c.el === e.target);
  if (!hit || !e.target.clientWidth) return;
  hit.chart.applyOptions({ width: e.target.clientWidth, height: e.target.clientHeight });
  hit.fit();
}));
// 캔들 + 거래량 + MA5/10/20/60. 반환한 fit 을 나중에 바꿔도 된다(c.fit = ...).
function makeChart(el, rows, timeVisible) {
  const chart = LightweightCharts.createChart(el, {
    width: el.clientWidth, height: el.clientHeight,
    layout: { background: { color: "#ffffff" }, textColor: "#374151", fontSize: 11 },
    grid: { vertLines: { color: "#f1f3f5" }, horzLines: { color: "#f1f3f5" } },
    rightPriceScale: { borderColor: "#e5e7eb", scaleMargins: { top: 0.08, bottom: 0.22 } },
    timeScale: { borderColor: "#e5e7eb", timeVisible, secondsVisible: false, rightOffset: 2 },
    crosshair: { mode: 0 },
    localization: { priceFormatter: p => Math.round(p).toLocaleString("ko-KR") },
  });
  const series = chart.addCandlestickSeries({
    upColor: TRADE_COLORS.buy, downColor: TRADE_COLORS.sell, borderUpColor: TRADE_COLORS.buy, borderDownColor: TRADE_COLORS.sell,
    wickUpColor: TRADE_COLORS.buy, wickDownColor: TRADE_COLORS.sell, priceLineVisible: false, lastValueVisible: false });
  series.setData(rows);
  const vol = chart.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
  chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  vol.setData(rows.map(b => ({ time: b.time, value: b.volume, color: b.close >= b.open ? "rgba(211,47,47,.35)" : "rgba(21,101,192,.35)" })));
  MA_LINES.forEach(m => chart.addLineSeries({ color: m.color, lineWidth: 1, priceLineVisible: false,
    lastValueVisible: false, crosshairMarkerVisible: false }).setData(sma(rows, m.period)));
  const c = { chart, series, el, fit: () => chart.timeScale().fitContent() };
  charts.push(c);
  chartResize.observe(el);
  return c;
}
// [[가격, 색, lineStyle, 제목], ...] 수평선. 가격이 없으면 건너뛴다.
function addPriceLines(series, lines) {
  lines.forEach(([price, color, lineStyle, title]) => price && series.createPriceLine(
    { price, color, lineWidth: 1, lineStyle, axisLabelVisible: true, title }));
}
// 체결 ✕: fills = [{time, side, price}], timeOf(fill) → 봉 시각
function addFillCrosses(series, rows, fills, timeOf = f => barAt(rows, f.time)) {
  series.attachPrimitive(new TradeCross(fills.map(f => ({ time: timeOf(f), side: f.side, price: f.price }))));
}
// 같은 봉·같은 방향 주문은 B/S 마커 하나로. label(g) — g = {side, qty, amt, labels}
function orderMarkers(rows, orders, label) {
  const m = new Map();
  for (const o of orders) {
    const time = barAt(rows, o.time), key = time + o.side;
    const g = m.get(key) || { time, side: o.side, qty: 0, amt: 0, labels: [] };
    g.qty += o.qty; g.amt += o.amt; g.labels.push(o.label); m.set(key, g);
  }
  return [...m.values()].map(g => ({
    time: g.time, position: g.side === "buy" ? "belowBar" : "aboveBar", shape: g.side === "buy" ? "arrowUp" : "arrowDown",
    color: TRADE_COLORS[g.side], text: label(g),
  }));
}

// -- 목록 ↔ 카드 이동 ---------------------------------------------------------
// 목록 행(tr[data-i]) 클릭 → 카드 #c{i}. 카드 머리 [↑ 목록](addBackButton) + 목록이 안 보이면 떠 있는 버튼.
// 돌아오면 방금 본 종목 행을 화면 가운데로 스크롤하고 잠깐 강조한다.
function backToList(i) {
  const summary = document.getElementById("summary");
  const row = i == null ? null : summary.querySelector(`tr[data-i="${i}"]`);
  if (!row) return summary.scrollIntoView({ behavior: "smooth", block: "start" });
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  summary.querySelectorAll("tr.seen").forEach(r => r.classList.remove("seen"));
  row.classList.add("seen");
  setTimeout(() => row.classList.remove("seen"), 2500);
}
function wireSummary() {
  const summary = document.getElementById("summary");
  summary.querySelectorAll("tbody tr").forEach(tr =>
    tr.addEventListener("click", () => document.getElementById("c" + tr.dataset.i).scrollIntoView({ behavior: "smooth" })));
  const totop = document.createElement("button");
  totop.className = "totop"; totop.textContent = "↑ 목록";
  document.body.appendChild(totop);
  let current = null;  // 화면 가운데에 걸친 카드
  totop.onclick = () => backToList(current);
  new IntersectionObserver(([e]) => totop.classList.toggle("show", !e.isIntersecting)).observe(summary);
  addEventListener("scroll", () => {
    const mid = innerHeight / 2;
    const hit = [...document.querySelectorAll(".card")].find(c => { const r = c.getBoundingClientRect(); return r.top <= mid && r.bottom >= mid; });
    current = hit ? hit.id.slice(1) : null;
  }, { passive: true });
}
function addBackButton(card, i) {
  const back = document.createElement("button");
  back.className = "back"; back.textContent = "↑ 목록"; back.onclick = () => backToList(i);
  card.querySelector(".head").appendChild(back);
}
