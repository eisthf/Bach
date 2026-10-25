"""무상증자 공시 이후 수익률 — 수정주가 일봉 + 공시일 1분봉.

반응일 D0: 공시 시각이 15:30 전(장전 포함)이면 그날, 15:30 이후·휴장일이면 다음 거래일.
기준가 B: D0 직전 거래일 종가(= 공시 전 마지막 종가).
권리락일 EX: 신주배정기준일 직전 거래일. 상장일 L: 신주 상장예정일 이후 첫 거래일.
수익률은 수정주가라 무상증자 희석이 보정돼 있다. 초과수익은 KODEX 코스닥150/200 대비.
"""
import statistics as st


def _ret(a, b):
    return None if a is None or b is None or not a else b / a - 1


def _first_on_or_after(bars, day):
    return next((i for i, b in enumerate(bars) if b['_date'] >= day), None)


def session(hm):
    return '장전' if hm < '09:00' else ('장중' if hm < '15:30' else '장후')


def event_metrics(e, bars, bench_bars):
    bench = {b['_date']: b['close'] for b in bench_bars}
    day, hm = e['time'][:10].replace('-', ''), e['time'][11:16]
    d0 = _first_on_or_after(bars, day)
    if d0 is not None and bars[d0]['_date'] == day and hm >= '15:30':
        d0 += 1
    if d0 is None or d0 >= len(bars) or d0 == 0:
        return None
    B = bars[d0 - 1]['close']
    c = lambda j: bars[j]['close'] if 0 <= j < len(bars) else None  # noqa: E731

    def bench_ret(i0, i1):
        if i1 >= len(bars):
            return None
        return _ret(bench.get(bars[i0]['_date']), bench.get(bars[i1]['_date']))

    rec = e.get('record_date')
    ex = _first_on_or_after(bars, rec) if rec else None
    ex = ex - 1 if ex is not None and ex - 1 > d0 else None
    lst = _first_on_or_after(bars, e['listing_date']) if e.get('listing_date') else None
    r = {
        'd0_date': bars[d0]['_date'], 'base_date': bars[d0 - 1]['_date'], 'base': B,
        'session': '휴장일' if bars[d0]['_date'] != day and hm < '15:30' else session(hm),
        'after_close': bars[d0 - 1]['_date'] == day,
        'gap': _ret(B, bars[d0]['open']), 'd0_high': _ret(B, bars[d0]['high']), 'd0': _ret(B, c(d0)),
        'd0_close_upper': c(d0) >= B * 1.295,
        'zero_vol_days': sum(1 for b in bars[d0:(ex or d0) + 1] if not b['volume']),
    }
    for k in (1, 2, 4, 9, 19, 39):
        v = _ret(B, c(d0 + k)) if d0 + k < len(bars) else None
        r[f'd{k + 1}'] = v
        br = bench_ret(d0 - 1, d0 + k)
        r[f'x{k + 1}'] = None if v is None else v - (br or 0)
    r['x0'] = r['d0'] - (bench_ret(d0 - 1, d0) or 0)
    if ex is not None:
        r.update({
            'ex_date': bars[ex]['_date'], 'days_to_ex': ex - d0,
            'cum_close': _ret(B, c(ex - 1)),                        # 권리 붙은 마지막 날 종가
            'cum_max': _ret(B, max(b['high'] for b in bars[d0:ex])),
            'd0close_to_cum': _ret(c(d0), c(ex - 1)),
            'ex_gap': _ret(c(ex - 1), bars[ex]['open']),
            'ex_high': _ret(c(ex - 1), bars[ex]['high']),
            'ex_day': _ret(c(ex - 1), c(ex)),
            'ex_open_to_close': _ret(bars[ex]['open'], c(ex)),
            'ex_to_ex5': _ret(c(ex - 1), c(ex + 4)) if ex + 4 < len(bars) else None,
            'ex_to_ex20': _ret(c(ex - 1), c(ex + 19)) if ex + 19 < len(bars) else None,
        })
    if lst is not None and 0 < lst < len(bars):
        r['listing'] = bars[lst]['_date']
        r['listing_day'] = _ret(c(lst - 1), c(lst))
        r['listing_to_l5'] = _ret(c(lst - 1), c(lst + 4)) if lst + 4 < len(bars) else None
    r['to_last'] = _ret(B, bars[-1]['close'])
    r['to_last_excess'] = r['to_last'] - (bench_ret(d0 - 1, len(bars) - 1) or 0)
    r['last_date'] = bars[-1]['_date']
    return r


def halt_metrics(e, r, m1):
    """장중 공시일 1분봉으로 매매정지·재개. 분봉은 원시 가격이라 같은 날 종가 배율로 수정가에 맞춘다."""
    day_hm = e['time'][11:16].replace(':', '')
    bars = [b for b in m1 if '0900' <= b['_hhmm'][:4] <= '1530']
    pre = [b for b in bars if b['_hhmm'][:4] <= day_hm]
    post = [b for b in bars if b['_hhmm'][:4] > day_hm]
    if not pre:
        return None
    if not post:
        return {'resume': None, 'halt_min': None, 'no_resume': True}
    P, R = pre[-1]['close'], post[0]['open']
    first = post[0]['_hhmm'][:4]
    halt = int(first[:2]) * 60 + int(first[2:]) - int(day_hm[:2]) * 60 - int(day_hm[2:])
    f = r['base'] * (1 + r['d0']) / post[-1]['close']
    out = {'resume': first, 'halt_min': halt, 'pre_move': P * f / r['base'] - 1, 'resume_gap': R / P - 1,
           'resume_vs_base': R * f / r['base'] - 1, 'post_high': max(b['high'] for b in post) / R - 1,
           'post_low': min(b['low'] for b in post) / R - 1, 'resume_to_close': post[-1]['close'] / R - 1}
    k = post[-1]['close'] / R
    out['resume_to_d5'] = (1 + r['d5']) / (1 + r['d0']) * k - 1 if r.get('d5') is not None else None
    out['resume_to_cum'] = (1 + r['d0close_to_cum']) * k - 1 if r.get('d0close_to_cum') is not None else None
    return out


KEYS = ['gap', 'd0_high', 'd0', 'x0', 'd5', 'x5', 'd10', 'd20', 'x20', 'd40', 'cum_close', 'cum_max',
        'd0close_to_cum', 'ex_gap', 'ex_high', 'ex_day', 'ex_open_to_close', 'ex_to_ex5', 'ex_to_ex20',
        'listing_day', 'listing_to_l5', 'to_last', 'to_last_excess',
        'halt_min', 'resume_gap', 'resume_vs_base', 'resume_to_close', 'resume_to_d5', 'resume_to_cum']
LABELS = {
    'gap': 'D0 시가 갭', 'd0_high': 'D0 고가', 'd0': 'D0 종가', 'x0': 'D0 종가(지수 대비)', 'd5': '5일', 'x5': '5일(지수 대비)',
    'd10': '10일', 'd20': '20일', 'x20': '20일(지수 대비)', 'd40': '40일', 'cum_close': '권리락 전날 종가',
    'cum_max': '공시~권리락 전 최고가', 'd0close_to_cum': 'D0 종가 매수→권리락 전날', 'ex_gap': '권리락일 시가 갭',
    'ex_high': '권리락일 고가', 'ex_day': '권리락일 종가', 'ex_open_to_close': '권리락일 시가→종가',
    'ex_to_ex5': '권리락 후 5일', 'ex_to_ex20': '권리락 후 20일', 'listing_day': '신주 상장일',
    'listing_to_l5': '상장 후 5일', 'to_last': '최근 종가', 'to_last_excess': '최근 종가(지수 대비)',
    'halt_min': '매매정지(분)', 'resume_gap': '재개가/공시 직전가', 'resume_vs_base': '재개가(기준 대비)',
    'resume_to_close': '재개가 매수→D0 종가', 'resume_to_d5': '재개가 매수→5일', 'resume_to_cum': '재개가 매수→권리락 전날',
}


def summary(rows, key):
    v = [r[key] for r in rows if r.get(key) is not None]
    if not v:
        return None
    return {'n': len(v), 'mean': st.mean(v), 'median': st.median(v), 'win': sum(x > 0 for x in v) / len(v),
            'min': min(v), 'max': max(v)}


def groups(rows):
    pure = [r for r in rows if not r['paid_too']]
    return {
        '순수 무상': pure,
        '1주 이상 배정': [r for r in pure if (r['ratio'] or 0) >= 1],
        '1주 미만 배정': [r for r in pure if (r['ratio'] or 0) < 1],
        '장중 공시': [r for r in pure if r['session'] == '장중'],
        '장후·장전·휴장일 공시': [r for r in pure if r['session'] != '장중'],
        '코스닥': [r for r in pure if r['market'] == 'KOSDAQ'],
        '유가': [r for r in pure if r['market'] == 'KOSPI'],
        '유무상': [r for r in rows if r['paid_too']],
    }
