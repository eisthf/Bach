// 대금 양봉 매매 페이지.
// [대금 양봉] 차트에서 [대금 양봉 매매 등록]한 종목이 '설정 중'으로 나타난다(조회일 봉 = 기준 캔들).
// 여러 거래일에 걸친 매매라 [상한가 매매] 목록(당일 상태머신)과 분리했다.
//   매입 구간: 기준 캔들 몸통(시가~종가) 양 끝에서 몸통 길이의 x%씩 안쪽
//   설정 중 ─[감시 시작]→ 기준일 다음 거래일부터 가격이 구간에 들어오면 1차 매수(기한 N거래일)
//   2차: 1차 다음 날부터, 구간 안이면서 2차 조건가 이하(혼합: 하단까지 여유 > u%면 −u%·갭의 k% 중 깊은 쪽 / 고정 −u% / 갭의 k%)
//   평단 +p% → 절반 매도, 남은 매수 계획 취소, 나머지는 고점 −t% 트레일링 스톱
//   전일 종가 < 구간 하단 → 그날 하단 아래 가격에서 전량 매도
//   [수동 전환]: 설정 중·감시 중·보유 중 언제든 엔진을 멈추고 수동 매매 화면으로 옮긴다.
import React, { useMemo } from 'react'
import { useStore } from '../store'
import { ROUTES, navigate } from '../router'
import CandleTradeCard, { ACTIVE } from '../components/CandleTradeCard'
import TradeChipBar, { useHiddenCards } from '../components/TradeChipBar'
import AccountViewFilter, { useAccountView } from '../components/AccountViewFilter'

export default function CandleTradePage() {
  const { accounts, candleTrades } = useStore()
  const rows = useMemo(() => {
    const out = []
    accounts.forEach((a) => {
      Object.values(candleTrades[a.id] || {}).filter((it) => (it.kind || 'candle') === 'candle')
        .forEach((it) => out.push({ account: a, item: it }))
    })
    // 운용 중 → 설정 중 → 종료 순, 같은 그룹은 등록 최신순.
    const rank = (p) => (ACTIVE.includes(p) ? 0 : p === 'DRAFT' ? 1 : 2)
    return out.sort((x, y) => rank(x.item.phase) - rank(y.item.phase)
      || String(y.item.created_at).localeCompare(String(x.item.created_at)))
  }, [accounts, candleTrades])
  const { isHidden, toggle } = useHiddenCards('bach.hidden.candleTrade')
  // 계좌 보기 필터(페이지별). 칩바·카드 모두 고른 계좌만.
  const { view, choose, visible } = useAccountView('bach.accountView.candleTrade', accounts)
  const inView = rows.filter(({ account }) => visible(account))
  const shown = inView.filter(({ account, item }) => !isHidden(account, item))

  return (
    <main className="candle-trade">
      <div className="candle-trade-intro">
        <strong>대금 양봉 매매</strong>
        <span className="muted">
          [대금 양봉]에서 종목 차트를 열고 <b>대금 양봉 매매 등록</b>을 누르면 조회일 봉을 기준 캔들로 여기에 추가됩니다.
          설정을 확인한 뒤 <b>감시 시작</b>을 누르면, 기준일 다음 거래일부터 가격이 매입 구간에 들어올 때 자동으로 매수합니다.
        </span>
        <button className="ghost" onClick={() => navigate(ROUTES.BIG_CANDLE)}>대금 양봉에서 고르기</button>
      </div>
      <AccountViewFilter accounts={accounts} rows={rows} view={view} choose={choose} />
      <TradeChipBar rows={inView} isHidden={isHidden} toggle={toggle} showAccount={accounts.length > 1} />
      {rows.length === 0 && (
        <div className="screener-empty">등록된 대금 양봉 매매 종목이 없습니다.</div>
      )}
      {rows.length > 0 && inView.length === 0 && (
        <div className="screener-empty">이 계좌에는 종목이 없습니다.</div>
      )}
      {inView.length > 0 && shown.length === 0 && (
        <div className="screener-empty">모든 종목이 숨김 상태입니다. 위 종목 배지를 누르면 다시 보입니다.</div>
      )}
      <div className="panels-col">
        {shown.map(({ account, item }) => (
          <CandleTradeCard key={`${account.id}:${item.code}`} account={account} item={item} />
        ))}
      </div>
    </main>
  )
}

