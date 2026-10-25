"""무상증자 공시 이벤트 스터디 — 수집 → 수익률 → 보고서(report.md) → 차트(charts.html).

조회 API만 쓰고 주문은 내지 않는다. 실행:
    cd backend && uv run python ../.claude/skills/bonus-issue-study/study.py --from 2026-01-01 --to 2026-10-16
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import metrics as M  # noqa: E402
import sources as S  # noqa: E402


def ymd(s):
    return s.replace('-', '')


def default_asof():
    now = dt.datetime.now()
    d = now.date() if now.hour >= 16 else now.date() - dt.timedelta(days=1)
    return d.strftime('%Y%m%d')


def pct(x, sign=True):
    return '—' if x is None else (f'{x * 100:+.1f}%' if sign else f'{x * 100:.0f}%')


def collect(args):
    print(f'[1/4] 공시 검색 {args.start}~{args.end} (정정·안내는 {args.asof}까지)', flush=True)
    events, skipped = S.build_events(args.start, args.end, args.asof, args.refresh)
    missing = [e['corp'] for e in events if e.get('ratio') is None or not e.get('record_date')]
    print(f'  {len(events)}건 (유무상 {sum(e["paid_too"] for e in events)}), 제외 {len(skipped)}건'
          + (f', 조건 못 읽음: {missing}' if missing else ''), flush=True)
    print('[2/4] 일봉(수정주가)', flush=True)
    daily = {}
    for code in sorted({e['code'] for e in events} | set(S.BENCH.values())):
        daily[code] = S.day_bars(code, args.start, args.asof, args.refresh)
    return events, skipped, daily


def analyze(args, events, daily):
    print('[3/4] 수익률·매매정지', flush=True)
    rows = []
    for e in events:
        bars = daily[e['code']]['bars']
        r = M.event_metrics(e, bars, daily[S.BENCH[e['market']]]['bars'])
        if r is None:
            print(f'  ! {e["corp"]}: 반응일 시세 없음 — 제외', flush=True)
            continue
        row = {k: e[k] for k in ('corp', 'code', 'market', 'time', 'title', 'paid_too', 'ratio', 'record_date',
                                 'listing_date', 'board_date', 'corrections', 'dart_rcpNo', 'kind_acptno',
                                 'ex_notice', 'halt_notice', 'notices')} | r
        if r['session'] == '장중':
            h = M.halt_metrics(e, r, S.min_bars(e['code'], ymd(e['time'][:10]), 1, args.asof))
            if h:
                row |= h
        rows.append(row)
    return rows


def write_report(args, rows, skipped, out):
    g = M.groups(rows)
    L = [f'# 무상증자 공시 이벤트 스터디 ({S.iso(args.start)} ~ {S.iso(args.end)})', '',
         f'조회일 {S.iso(args.asof)}. 조회 전용(주문 없음). 생성: `.claude/skills/bonus-issue-study/study.py`.', '',
         '## 데이터', '',
         '- 공시: KIND 공시검색 \'무상증자\'(시각), DART 최종 정정본 본문(배정비율·기준일·상장예정일). '
         '유가·코스닥 상장사의 무상·유무상증자 결정 **최초 공시**만. 정정 공시·기타법인·종속회사 공시는 제외.',
         '- 시세: 키움 ka10081 수정주가 일봉. 장중 공시는 그날 1분봉(원시 가격 → 같은 날 종가 배율로 환산)으로 매매정지·재개가.',
         '- 기준가 B = 공시 전 마지막 종가. D0 = 장전·장중 공시면 그날, 장후·휴장일이면 다음 거래일. N일 = D0를 1일째로 센 종가.',
         '- 권리락일 = 신주배정기준일 직전 거래일. 지수 대비 = KODEX 코스닥150(229200) / KODEX 200(069500).',
         '- 세금·수수료·슬리피지 제외. 재개 단일가 체결 가능 수량은 확인하지 않음.', '',
         f'총 {len(rows)}건: 순수 무상 {len(g["순수 무상"])}건, 유무상 {len(g["유무상"])}건.', '']
    if skipped:
        L += ['제외: ' + ', '.join(f'{s["corp"]}({s["time"][:10]}, {s["why"]})' for s in skipped), '']
    L += ['## 그룹별 요약', '', '평균 / 중앙 / 상승 비율 (n). 순수 무상 기준, 마지막 열만 유무상.', '']
    names = list(g)
    show = ['d0', 'x0', 'd5', 'd20', 'x20', 'cum_close', 'cum_max', 'd0close_to_cum', 'ex_gap', 'ex_day',
            'ex_open_to_close', 'ex_to_ex20', 'listing_day', 'to_last', 'to_last_excess',
            'halt_min', 'resume_vs_base', 'resume_to_close', 'resume_to_cum']
    L += ['| 지표 | ' + ' | '.join(f'{n} ({len(g[n])})' for n in names) + ' |',
          '|---|' + '---|' * len(names)]
    for k in show:
        cells = []
        for n in names:
            s = M.summary(g[n], k)
            if not s:
                cells.append('—')
            elif k == 'halt_min':
                cells.append(f'{s["median"]:.0f}분 (n={s["n"]})')
            else:
                cells.append(f'{pct(s["mean"])} / {pct(s["median"])} / {pct(s["win"], False)} ({s["n"]})')
        L.append(f'| {M.LABELS[k]} | ' + ' | '.join(cells) + ' |')
    L += ['', '## 종목별 (수정주가)', '',
          '재개가 = 장중 공시 후 매매 재개 첫 체결(기준 대비). 권리락일 = 권리락 전날 종가 대비 종가. 상 = D0 종가 상한가.', '',
          '| 공시 시각 | 종목 | 시장 | 1주당 | 구분 | 정지·재개 | 재개가 | D0 | 5일 | 20일 | 권리락 전날 | 권리락일 | 권리락 후 20일 | 상장일 | 최근 | 최근(지수 대비) |',
          '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for r in rows:
        if r.get('resume'):
            halt = f'{r["halt_min"]}분 → {r["resume"][:2]}:{r["resume"][2:]}'
        elif r.get('no_resume'):
            halt = '당일 재개 없음'
        else:
            halt = r['session']
        L.append(f'| {r["time"]} | {r["corp"]} ({r["code"]}) | {"코스닥" if r["market"] == "KOSDAQ" else "유가"} | '
                 f'{r["ratio"] if r["ratio"] is not None else "?"} | {"유무상" if r["paid_too"] else "무상"} | {halt} | '
                 f'{pct(r.get("resume_vs_base"))} | {pct(r["d0"])}{" 상" if r["d0_close_upper"] else ""} | '
                 f'{pct(r.get("d5"))} | {pct(r.get("d20"))} | {pct(r.get("cum_close"))} | {pct(r.get("ex_day"))} | '
                 f'{pct(r.get("ex_to_ex20"))} | {pct(r.get("listing_day"))} | {pct(r["to_last"])} | {pct(r["to_last_excess"])} |')
    flags = [r for r in rows if r['zero_vol_days']]
    if flags:
        L += ['', '거래량 0인 날(거래정지 가능성)이 있는 종목: '
              + ', '.join(f'{r["corp"]} {r["zero_vol_days"]}일' for r in flags)]
    (out / 'report.md').write_text('\n'.join(L) + '\n', encoding='utf-8')


def write_charts(args, rows, daily, out):
    print('[4/4] 차트(3분봉)', flush=True)
    data = []
    for r in rows:
        if r['paid_too'] and not args.include_paid:
            continue
        bars = daily[r['code']]['bars']
        dates = [b['_date'] for b in bars]
        d0 = dates.index(r['d0_date'])
        ex = dates.index(r['ex_date']) if r.get('ex_date') in dates else None
        lo, hi = max(0, d0 - 20), min(len(bars), (ex + 21) if ex is not None else len(bars))
        day = ymd(r['time'][:10])
        i = next(j for j, d in enumerate(dates) if d >= day)
        days3 = dates[max(0, i - 1):i + (2 if dates[i] == day else 1)]
        m3 = []
        for d in days3:
            m3 += S.min_bars(r['code'], d, 3, args.asof)
        m3.sort(key=lambda b: b['time'])
        stats = {k: r.get(k) for k in ('d0', 'd5', 'd20', 'cum_close', 'ex_day', 'ex_to_ex20', 'listing_day',
                                       'resume_vs_base', 'resume_to_cum')}
        data.append({
            'corp': r['corp'], 'code': r['code'], 'market': '코스닥' if r['market'] == 'KOSDAQ' else '유가',
            'time': r['time'], 'session': r['session'], 'ratio': r['ratio'], 'paid_too': r['paid_too'],
            'record_date': r['record_date'], 'listing_date': r['listing_date'], 'ex_date': r.get('ex_date'),
            'ex_notice': r['ex_notice'], 'd0': r['d0_date'], 'base': r['base'],
            'resume': r.get('resume'), 'halt_min': r.get('halt_min'), 'stats': stats,
            'daily': [[b['_date'], b['open'], b['high'], b['low'], b['close'], b['volume']] for b in bars[lo:hi]],
            'daily_truncated': ex is None or ex + 21 > len(bars),
            'm3': [[b['time'], b['open'], b['high'], b['low'], b['close'], b['volume']] for b in m3],
            'days3': days3})
    meta = {'start': S.iso(args.start), 'end': S.iso(args.end), 'asof': S.iso(args.asof),
            'last': S.iso(max(b['_date'] for b in daily[S.BENCH['KOSDAQ']]['bars'])),
            'n': len(data), 'paid': args.include_paid}
    tpl = (HERE / 'template.html').read_text(encoding='utf-8')
    html = (tpl.replace('/*__DATA__*/null', json.dumps(data, ensure_ascii=False, separators=(',', ':')))
               .replace('/*__META__*/null', json.dumps(meta, ensure_ascii=False)))
    (out / 'charts.html').write_text(html, encoding='utf-8')
    print(f'  charts.html {len(data)}건, {len(html) // 1024} KB', flush=True)


def main():
    p = argparse.ArgumentParser(description='무상증자 공시 이벤트 스터디(조회 전용)')
    p.add_argument('--from', dest='start', help='최초 공시 시작일 YYYY-MM-DD(기본: 올해 1월 1일)')
    p.add_argument('--to', dest='end', help='최초 공시 종료일(기본: 조회일)')
    p.add_argument('--asof', help='시세·정정·안내를 볼 마지막 날(기본: 16시 이후 오늘, 아니면 어제)')
    p.add_argument('--out', help='출력 폴더(기본 research/bonus_issue/<from>_<to>/)')
    p.add_argument('--include-paid', action='store_true', help='차트에 유무상증자도 넣기')
    p.add_argument('--no-charts', action='store_true', help='차트(3분봉 조회) 생략')
    p.add_argument('--refresh', action='store_true', help='목록·일봉 캐시 무시(공시 본문·지난 분봉은 유지)')
    args = p.parse_args()
    args.asof = ymd(args.asof) if args.asof else default_asof()
    args.start = ymd(args.start) if args.start else args.asof[:4] + '0101'
    args.end = min(ymd(args.end), args.asof) if args.end else args.asof
    out = Path(args.out) if args.out else S.REPO / 'research' / 'bonus_issue' / f'{args.start}_{args.end}'
    out.mkdir(parents=True, exist_ok=True)

    events, skipped, daily = collect(args)
    rows = analyze(args, events, daily)
    (out / 'events.json').write_text(json.dumps(events, ensure_ascii=False, indent=1), encoding='utf-8')
    (out / 'analysis.json').write_text(json.dumps({'rows': rows, 'skipped': skipped, 'summary': {
        n: {k: M.summary(v, k) for k in M.KEYS} for n, v in M.groups(rows).items()}},
        ensure_ascii=False, indent=1), encoding='utf-8')
    write_report(args, rows, skipped, out)
    if not args.no_charts:
        write_charts(args, rows, daily, out)
    print(f'완료: {out}', flush=True)


if __name__ == '__main__':
    main()
