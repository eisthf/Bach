"""무상증자 공시·시세 수집 — 조회 전용(주문 API 없음).

- KIND 공시검색: 공시 시각(분), 종목코드, 매매거래정지·권리락·추가상장 안내
- DART: 최종 정정본 본문 → 1주당 배정주식수·신주배정기준일·상장예정일
- 키움 ka10081 수정주가 일봉, ka10080 1분봉·3분봉(원시 가격)

캐시는 ``backend/backtest_data/bonus_issue/``(git 미추적). 목록 조회는 조회일(asof)별,
공시 본문·과거 분봉은 영구 캐시.
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[3]
BACKEND = REPO / 'backend'
load_dotenv(BACKEND / '.env')
sys.path.insert(0, str(BACKEND))
CACHE = BACKEND / 'backtest_data' / 'bonus_issue'
UA = {'User-Agent': 'Mozilla/5.0'}
BENCH = {'KOSDAQ': '229200', 'KOSPI': '069500'}  # KODEX 코스닥150 / KODEX 200


def _cache(*parts):
    p = CACHE.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def strip(s):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', s))).strip()


def iso(d):
    return f'{d[:4]}-{d[4:6]}-{d[6:8]}'


# ---------------------------------------------------------------- KIND
# KIND는 빈 검색 필드까지 전부 보내야 오류 페이지 대신 목록을 준다.
_KIND_EMPTY = {f'{p}disclosureType{n:02d}': '' for p in ('', 'p') for n in (*range(1, 12), 13, 14, 20)}
_KIND_EMPTY.update(dict.fromkeys((
    'searchCodeType', 'repIsuSrtCd', 'allRepIsuSrtCd', 'oldSearchCorpName', 'disclosureType', 'disTypevalue',
    'reportCd', 'searchCorpName', 'business', 'marketType', 'settlementMonth', 'securities', 'submitOblgNm',
    'enterprise', 'reportNmPop'), ''))


def kind_search(keyword, start, end, asof, refresh=False):
    """KIND 공시검색 결과(최신순). start/end = YYYYMMDD."""
    path = _cache('lists', f'kind_{keyword}_{start}_{end}_{asof}.json')
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding='utf-8'))
    rows = []
    for page in range(1, 50):
        r = requests.post('https://kind.krx.co.kr/disclosure/details.do', headers=UA, timeout=30, data={
            **_KIND_EMPTY, 'method': 'searchDetailsSub', 'currentPageSize': 100, 'pageIndex': page,
            'orderMode': 1, 'orderStat': 'D', 'forward': 'details_sub', 'reportNm': keyword,
            'reportNmTemp': keyword, 'fromDate': iso(start), 'toDate': iso(end), 'bfrDsclsType': 'on'})
        r.encoding = 'utf-8'
        if '페이지 오류' in r.text[:3000]:
            raise RuntimeError(f'KIND 검색 오류 페이지({keyword} {page}쪽)')
        got = 0
        for tr in re.findall(r'<tr[\s\S]*?</tr>', r.text):
            tds = re.findall(r'<td[\s\S]*?</td>', tr)
            acpt = re.search(r"openDisclsViewer\('(\d+)'", tr)
            if len(tds) < 5 or not acpt:
                continue
            code = re.search(r"companysummary_open\('(\w+)'", tr)
            mkt = re.search(r"icn_t_(\w+)\.gif", tr)
            got += 1
            rows.append({'time': strip(tds[1]), 'corp': strip(tds[2]), 'title': strip(tds[3]),
                         'submitter': strip(tds[4]), 'acptno': acpt.group(1),
                         'code5': code.group(1) if code else None, 'mkt': mkt.group(1) if mkt else None})
        if got < 100:
            break
        time.sleep(.5)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
    return rows


# ---------------------------------------------------------------- DART
def dart_search(keyword, start, end, asof, refresh=False):
    """DART 공시통합검색(정정 전 원본 포함). detailSearch.do는 막히고 .ax만 된다."""
    path = _cache('lists', f'dart_{keyword}_{start}_{end}_{asof}.json')
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding='utf-8'))
    s = requests.Session()
    s.headers.update(UA)
    s.get('https://dart.fss.or.kr/dsab007/main.do', timeout=30)
    rows = []
    for page in range(1, 50):
        r = s.post('https://dart.fss.or.kr/dsab007/detailSearch.ax', timeout=30, data={
            'currentPage': page, 'maxResults': 100, 'maxLinks': 10, 'sort': 'date', 'series': 'desc',
            'option': 'report', 'reportNamePopYn': 'N', 'reportName': keyword, 'startDate': start,
            'endDate': end, 'businessCode': 'all', 'corporationType': 'all', 'closingAccountsMonth': 'all'})
        r.encoding = 'utf-8'
        got = 0
        for tr in re.findall(r'<tr[\s\S]*?</tr>', r.text):
            rcp = re.search(r'rcpNo=(\d+)', tr)
            tds = re.findall(r'<td[\s\S]*?</td>', tr)
            corp = re.search(r"openCorpInfoNew\('(\d+)'[\s\S]*?>\s*([^<]+?)\s*</a>", tr)
            if not rcp or len(tds) < 5 or not corp:
                continue
            got += 1
            rows.append({'corp': corp.group(2).strip(), 'corp_code': corp.group(1), 'report': strip(tds[2]),
                         'rcpNo': rcp.group(1), 'date': strip(tds[4])})
        if got < 100:
            break
        time.sleep(.5)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
    return rows


def dart_doc(rcp):
    """공시 본문 중 '증자' 목차들의 텍스트(영구 캐시)."""
    path = _cache('dart', f'{rcp}.txt')
    if path.exists():
        return path.read_text(encoding='utf-8')
    m = requests.get(f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp}', headers=UA, timeout=30).text
    texts = []
    for blk in re.findall(r"node1\['text'\][\s\S]*?node1\['dtd'\] = \"[^\"]*\"", m):
        kv = dict(re.findall(r"node1\['(\w+)'\] = \"([^\"]*)\"", blk))
        if '증자' not in kv.get('text', ''):
            continue
        r = requests.get('https://dart.fss.or.kr/report/viewer.do', headers=UA, timeout=30, params={
            k: kv[k] for k in ('rcpNo', 'dcmNo', 'eleId', 'offset', 'length', 'dtd')})
        try:
            body = r.content.decode('utf-8')
        except UnicodeDecodeError:
            body = r.content.decode('cp949', errors='replace')
        texts.append(strip(body))
        time.sleep(.4)
    text = '\n'.join(texts)
    if text:
        path.write_text(text, encoding='utf-8')
    time.sleep(.4)
    return text


def _ymd(s):
    m = re.search(r'(\d{4})\s*[년.-]\s*(\d{1,2})\s*[월.-]\s*(\d{1,2})', s or '')
    return f'{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}' if m else None


_DATE = r'(\d{4}(?:년 |\.)\d{2}(?:월 |\.)\d{2})'


def parse_terms(text):
    """무상 부분의 조건. 유무상 공시는 'Ⅱ. 무상증자' 이후 구간에서 찾는다. 날짜는 '2026년 05월 27일'·'2026.05.27' 둘 다."""
    i = text.find('Ⅱ. 무상증자')
    seg = text[i:] if i >= 0 else text
    ratio = re.search(r'1주당 ?신주배정 ?주식수 보통주식 ?\(주\) ([\d.,]+)', seg)
    base = re.search(r'신주배정기준일 ' + _DATE, seg)
    listing = re.search(r'신주의 상장 ?예정일 ' + _DATE, seg)
    board = re.search(r'이사회결의일\(결정일\) ' + _DATE, seg)
    before = re.search(r'증자전 발행주식총수 (?:\(주\) )?보통주식 \(주\) ([\d,]+)', seg)
    return {'ratio': float(ratio.group(1).replace(',', '')) if ratio else None,
            'record_date': _ymd(base.group(1)) if base else None,
            'listing_date': _ymd(listing.group(1)) if listing else None,
            'board_date': _ymd(board.group(1)) if board else None,
            'shares_before': int(before.group(1).replace(',', '')) if before else None}


def build_events(start, end, asof, refresh=False):
    """[start, end]에 최초 공시된 유가·코스닥 무상/유무상증자 결정. 정정·안내는 asof까지 본다."""
    kind = kind_search('무상증자', start, asof, asof, refresh)
    exr = kind_search('권리락', start, asof, asof, refresh)  # 유가는 '권리락 기준가격 안내'라 '무상증자'로 안 걸림
    dart = dart_search('무상증자', start, asof, asof, refresh)
    events, skipped = [], []
    for r in kind:
        t = r['title']
        if '무상증자' not in t or '결정' not in t or t.startswith('[정정]'):
            continue
        day = r['time'][:10].replace('-', '')
        if not (start <= day <= end):
            continue
        if '종속회사' in t or r['mkt'] not in ('ko', 'yu'):
            skipped.append({'corp': r['corp'], 'time': r['time'], 'title': t,
                            'why': '종속회사 공시' if '종속회사' in t else f"시장 {r['mkt']}"})
            continue
        code = r['code5'] + '0' if r['code5'] and len(r['code5']) == 5 else r['code5']
        key = r['corp'].replace(' ', '')
        cands = [d for d in dart if d['corp'].replace(' ', '') == key and '결정' in d['report']
                 and '종속회사' not in d['report'] and d['rcpNo'][:8] >= day]
        latest = max(cands, key=lambda d: d['rcpNo']) if cands else None
        terms = parse_terms(dart_doc(latest['rcpNo'])) if latest else {}
        notices = [x for x in kind + exr if x['corp'] == r['corp'] and x['time'] >= r['time'] and x is not r
                   and not x['title'].startswith('[정정]') and '결정' not in x['title'] and '유상' not in x['title']]
        notices = sorted({(x['time'], x['title']): x for x in notices}.values(), key=lambda x: x['time'])
        ex_notice = next((x for x in notices if '권리락' in x['title']), None)
        events.append({
            'corp': r['corp'], 'code': code, 'market': 'KOSDAQ' if r['mkt'] == 'ko' else 'KOSPI',
            'time': r['time'], 'title': t, 'kind_acptno': r['acptno'], 'paid_too': '유무상' in t,
            'dart_rcpNo': latest['rcpNo'] if latest else None,
            'corrections': len(cands) - 1 if cands else 0, **terms,
            'ex_notice': {'time': ex_notice['time'], 'title': ex_notice['title']} if ex_notice else None,
            'halt_notice': any('거래정지' in x['title'] for x in notices),
            'notices': [{'time': x['time'], 'title': x['title']} for x in notices]})
    events.sort(key=lambda e: e['time'])
    return events, skipped


# ---------------------------------------------------------------- 키움(조회만)
_token = None


def token():
    global _token
    if _token is None:
        from app.providers import kiwoom_api as k
        _token = k.get_access_token(os.environ['REAL_APPKEY'], os.environ['REAL_SECRETKEY'], mock=False)
    return _token


def day_bars(code, start, asof, refresh=False):
    """수정주가 일봉 [start-90일, asof]. 캐시는 조회일별."""
    from app.providers import kiwoom_api as k
    path = _cache('daily', f'{code}_{asof}.json')
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding='utf-8'))
    span = (dt.date.fromisoformat(iso(asof)) - dt.date.fromisoformat(iso(start))).days + 90
    bars = k.fetch_day_bars(token(), code, mock=False, today=asof, lookback_extra=int(span * 0.72) + 30,
                            max_pages=30)
    name = k.fetch_stock_name(token(), code, mock=False)
    data = {'code': code, 'name': name, 'bars': [
        {x: b[x] for x in ('_date', 'open', 'high', 'low', 'close', 'volume', 'amount')}
        for b in bars if b['_date'] <= asof]}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    time.sleep(.2)
    return data


def min_bars(code, day, scope, today):
    """그날 분봉(원시 가격 — 이후 권리락이 반영되지 않을 수 있다). 지난 날만 영구 캐시."""
    from app.providers import kiwoom_api as k
    path = _cache(f'm{scope}', f'{code}_{day}.json')
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    try:
        bars = k.fetch_min_bars_on(token(), code, day, tic_scope=scope, mock=False, max_pages=40)
    except Exception as ex:  # noqa: BLE001
        print(f'  ! {code} {day} {scope}분봉 실패: {ex}', flush=True)
        return []
    rows = [{x: b[x] for x in ('time', '_hhmm', 'open', 'high', 'low', 'close', 'volume')} for b in bars]
    if day < today:
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
    time.sleep(.2)
    return rows
