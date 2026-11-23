"""무상증자 공시 감시 → 자동 매수.

감시(``BonusWatcher``, 전 계좌 공유)
  KIND 공시검색('무상증자')을 ``POLL_SEC``(기본 60초, ``BONUS_POLL_SEC``)마다 조회해 새 접수번호를
  찾는다. 키움 REST에는 공시 API가 없다. 대상 = 제목에 '무상증자'·'결정'이 있고 정정·종속회사 공시가
  아닌 유가·코스닥 최초 공시. 유무상증자(``paid_too``)는 따로 표시한다(지난 조사에서 권리락 전날까지
  중앙 −39%). 조회 범위는 직전 평일~오늘 — 장후·주말 공시를 다음 조회일 아침에도 잡는다.
  자주 볼 필요가 없다: 장중 공시는 공시와 같은 분에 매매정지가 걸려 재개 단일가(약 40분 뒤)까지
  체결이 없고, 목표 시각은 감지 시각이 아니라 공시 시각에서 계산한다.

매수(``BonusTradeManager``, 계좌별)
  계좌별 스위치(기본 꺼짐)가 켜져 있으면 공시 종목을 **수동 매매 목록**(origin "bonus" — 무상증자
  화면에만 보인다)에 올리고 매수 작업(``BuyJob``)을 돌린다. ``BuyJob``은 그 종목 카드의 기록이기도 하다
  (공시·권리락 일정·갭상승 계획). 손절·익절·보유·평단은 수동 매매와 같은 규칙(``manual_trade.ExitRule``,
  계좌 잔고 기준)이다 — 기본 평단 −15% / +20%(전량, 또는 절반 매도 뒤 고점 −5% 트레일링).
  매수 조건에 안 맞는 공시(유무상 제외, 이미 재개됨, 시가 상한 초과 등)도 카드는 올린다 — 손절·익절을
  끈 '수동' 상태로. 카드는 사람이 [수동 매매 화면으로]·[구간 매매로]·[삭제]하기 전까지 남는다.

  장중 공시는 보통 매매정지 30분 + 재개 단일가 10분 = 공시 후 약 40분(``resume_min``)에 첫
  체결이 난다(``kind`` "intraday" — 그대로 산다). 장전 공시는 그날 09:00, 장후·휴일·재개가 15:20을
  넘는 공시는 다음 평일 09:00 단일가가 목표(``target_at``)이고(``kind`` "open"), 시가 단일가 예상가가
  기준가 대비 ``open_max_pct``(기본 10%)를 넘지 않을 때만 산다 — 주문 방식이 시장가·상한가여도 시가
  예상가를 확인한 뒤 낸다. 휴장일은 모른다 — 그날 못 사면 기한(목표일 15:30)에 끝난다.
  감지했을 때 목표 시각이 이미 지났으면(서버가 꺼져 있던 동안 재개됨) 사지 않고 '건너뜀'으로 남긴다.
  1주당 배정(DART 본문)이 ``min_ratio``(기본 1주, 0이면 보지 않음) 이상일 때만 산다. 본문은 감지 뒤
  백그라운드로 읽으므로 그 전엔 주문을 기다리고, 읽은 즉시 기준 미달이면(또는 끝내 못 읽으면) 주문 시각을
  기다리지 않고 '건너뜀' — 카드는 손절·익절을 끈 수동으로 남는다.

  카드 상태: 매수 대기 → 산 뒤 '자동'(손절·익절 감시) / '수동'(둘 다 끔, 사람이 매매) / '갭상승'.
  [수동으로]는 언제든(매수 대기 취소 포함). 수동에서 켜는 **갭상승 모드**(``GapPlan``)는 켠 날(D) 다음
  거래일부터 매일 장 시작 직후 일봉으로 시가를 보고, 시가 ≥ 전일 종가 × (1 + g%)(옵션: 전일이 양봉이면
  전일 시가 기준)면 금액만큼 시장가로 사고 갭상승용 손절·익절(기본 −3% / +10%) 감시로 넘긴다.
  안 맞으면 다음 날 다시 본다(사람이 끌 때까지).

  권리락 일정: 공시를 감지하면 DART 본문에서 1주당 배정·신주배정기준일·상장예정일을 읽어 권리락일
  (= 기준일 직전 평일, 예정)을 정하고, KIND '권리락' 안내(기준일 2평일 전 장후에 나온다)가 오면
  다음 평일로 확정한다. 화면은 카드 일봉에 공시·권리락 안내·권리락일을 표시한다.

  주문 방식(``order_mode``):
    expected     (기본) 목표 시각 ``lead_sec``초 전 예상체결가로 수량(예산 ÷ 예상가)을 정해
                 상한가 지정가로 낸다 — 단일가에서 체결가로 체결된다. 마감 직전 예상가가 더
                 오르면 예산을 조금 넘을 수 있다. 예상가가 없거나 그보다 먼저 체결이 나면
                 after_resume 로 넘어간다.
    upper_limit  감지 즉시 상한가 지정가, 수량 = 예산 ÷ 상한가. 예산을 넘지 않지만 덜 산다.
    market       감지 즉시 시장가, 수량 = 예산 ÷ 공시 전 현재가. 재개가가 뛰면 예산을 넘는다.
    after_resume 재개 뒤 첫 체결가로 수량을 정해 시장가. 재개 단일가에는 못 들어간다.
  주문이 거부되면(매매정지 중 등) ``RETRY_SEC``마다 다시 낸다. 다시 내기 전에 계좌 보유·미체결을
  확인해 같은 종목을 두 번 사지 않는다. ``skip_above_pct``(>0)면 체결 기준가(예상가·첫 체결가)가
  기준가 대비 그 % 이상일 때 사지 않는다.

  재시작하면 기다리던 작업을 이어간다(``state.{계좌}.bonus.json``).

  09:00 직후 [상한가 매매]와 겹치지 않게: 키움 REST 게이트는 프로세스 전역(0.35초 간격, 주문 우선)이라
  무상증자 주문·예상가 조회가 상한가 1차 주문의 슬롯을 먼저 잡을 수 있다. 어느 계좌든 상한가 매매에
  1차 주문을 아직 못 낸 자동매매 종목이 있으면 키움 호출 전에 기다린다(최대 ``ULC_YIELD_MAX_SEC``).
  KIND 조회는 키움 게이트를 거치지 않지만 08:59:30~09:01:30엔 쉰다(``QUIET``). 화면 방송(WS)은 새 공시나
  감시 상태(정상↔오류)가 바뀔 때만 한다.

  데모(수동 장 시계, 키움 계좌 없음)에서는 요일·장 시간을 따지지 않는다: 목표 = 공시 + ``resume_min``분,
  주문 접수는 언제나, 기한은 목표 + 하루. 재개 감지는 [장 시작]을 누른 상태에서만 된다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import html
import time as _time
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

from .manual_trade import ExitRule
from .market_clock import KST, REGULAR_CLOSE, REGULAR_OPEN, now_kst
from .providers.base import DAY_INTERVAL

if TYPE_CHECKING:
    from .hub import AccountManager, Hub

logger = logging.getLogger("bach.bonus_issue")

ORDER_MODES = ("expected", "upper_limit", "market", "after_resume")
ACCEPT_FROM = time(8, 30)        # 장전 동시호가 주문 접수 시작
LAST_RESUME = time(15, 20)       # 이보다 늦게 재개되면 그날은 체결이 없다고 본다
ACTIVE_JOB = ("waiting", "ordering")


# ---------------------------------------------------------------- 공시
@dataclass
class Disclosure:
    acptno: str
    disclosed_at: str            # ISO(KST, 분 단위)
    corp: str
    title: str
    code: str = ""
    market: str = ""             # KOSPI / KOSDAQ / 기타
    paid_too: bool = False       # 유무상증자
    eligible: bool = True        # 매수 대상(유가·코스닥 최초 결정 공시)
    why: str = ""                # 대상이 아닌 이유

    @property
    def at(self) -> datetime:
        return datetime.fromisoformat(self.disclosed_at)


def classify(row: dict) -> Optional[Disclosure]:
    """KIND 목록 한 줄 → Disclosure. 무상증자 결정 공시가 아니면 None."""
    title = row.get("title") or ""
    if "무상증자" not in title or "결정" not in title:
        return None
    if re.match(r"^\[[^\]]*정정\]", title):
        return None  # 정정 공시 — 최초 공시만 본다
    try:
        at = datetime.strptime(row["time"].strip()[:16], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    except (KeyError, ValueError):
        return None
    code = row.get("code5") or ""
    if len(code) == 5:
        code += "0"
    mkt = {"yu": "KOSPI", "ko": "KOSDAQ"}.get(row.get("mkt") or "", row.get("mkt") or "")
    d = Disclosure(acptno=str(row["acptno"]), disclosed_at=at.isoformat(), corp=row.get("corp") or "",
                   title=title, code=code, market=mkt, paid_too="유무상" in title)
    if "종속회사" in title:
        d.eligible, d.why = False, "종속회사 공시"
    elif mkt not in ("KOSPI", "KOSDAQ"):
        d.eligible, d.why = False, f"시장 {mkt or '미상'}"
    elif not re.fullmatch(r"\d{6}", code):
        d.eligible, d.why = False, "종목코드 없음"
    return d


# KIND는 빈 검색 필드까지 전부 보내야 오류 페이지 대신 목록을 준다(.claude/skills/bonus-issue-study).
_KIND_EMPTY = {f"{p}disclosureType{n:02d}": "" for p in ("", "p") for n in (*range(1, 12), 13, 14, 20)}
_KIND_EMPTY.update(dict.fromkeys((
    "searchCodeType", "repIsuSrtCd", "allRepIsuSrtCd", "oldSearchCorpName", "disclosureType", "disTypevalue",
    "reportCd", "searchCorpName", "business", "marketType", "settlementMonth", "securities", "submitOblgNm",
    "enterprise", "reportNmPop"), ""))


def _strip(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def fetch_kind(start: date, end: date, keyword: str = "무상증자") -> List[dict]:
    """(블로킹) KIND 공시검색 — [start, end] 공시 목록(최신순, 한 쪽 100건). 실패는 예외.

    '무상증자'로는 결정 공시 말고도 매매정지·추가상장·코스닥 권리락 안내가 걸리지만 하루 몇 건이라
    며칠 범위도 한 쪽이면 된다. 유가 권리락 안내는 제목이 '권리락 기준가격 안내'라 '권리락'으로 찾는다.
    """
    import requests

    r = requests.post("https://kind.krx.co.kr/disclosure/details.do", timeout=15,
                      headers={"User-Agent": "Mozilla/5.0"}, data={
        **_KIND_EMPTY, "method": "searchDetailsSub", "currentPageSize": 100, "pageIndex": 1,
        "orderMode": 1, "orderStat": "D", "forward": "details_sub", "reportNm": keyword,
        "reportNmTemp": keyword, "fromDate": start.isoformat(), "toDate": end.isoformat(),
        "bfrDsclsType": "on"})
    r.raise_for_status()
    r.encoding = "utf-8"
    if "페이지 오류" in r.text[:3000]:
        raise RuntimeError("KIND 검색 오류 페이지")
    rows = []
    for tr in re.findall(r"<tr[\s\S]*?</tr>", r.text):
        tds = re.findall(r"<td[\s\S]*?</td>", tr)
        acpt = re.search(r"openDisclsViewer\('(\d+)'", tr)
        if len(tds) < 5 or not acpt:
            continue
        code = re.search(r"companysummary_open\('(\w+)'", tr)
        mkt = re.search(r"icn_t_(\w+)\.gif", tr)
        rows.append({"time": _strip(tds[1]), "corp": _strip(tds[2]), "title": _strip(tds[3]),
                     "acptno": acpt.group(1), "code5": code.group(1) if code else None,
                     "mkt": mkt.group(1) if mkt else None})
    return rows


def _ymd(s: str) -> str:
    m = re.search(r"(\d{4})\s*[년.-]\s*(\d{1,2})\s*[월.-]\s*(\d{1,2})", s or "")
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""


_DATE = r"(\d{4}(?:년 |\.)\d{2}(?:월 |\.)\d{2})"


def parse_terms(text: str) -> dict:
    """DART 무상증자 결정 본문 → 1주당 배정·신주배정기준일·상장예정일. 유무상은 'Ⅱ. 무상증자' 뒤에서 찾는다."""
    i = text.find("Ⅱ. 무상증자")
    seg = text[i:] if i >= 0 else text
    ratio = re.search(r"1주당 ?신주배정 ?주식수 보통주식 ?\(주\) ([\d.,]+)", seg)
    base = re.search(r"신주배정기준일 " + _DATE, seg)
    listing = re.search(r"신주의 상장 ?예정일 " + _DATE, seg)
    return {"ratio": float(ratio.group(1).replace(",", "")) if ratio else 0.0,
            "record_date": _ymd(base.group(1)) if base else "",
            "listing_date": _ymd(listing.group(1)) if listing else ""}


def fetch_terms(corp: str, day: date) -> dict:
    """(블로킹) DART 공시통합검색에서 그날 그 회사의 무상증자 결정 공시를 찾아 본문 조건을 읽는다.

    못 찾으면 {}(아직 DART에 안 올라옴 — 나중에 다시). 네트워크 오류는 예외.
    (.claude/skills/bonus-issue-study/sources.py 와 같은 경로 — detailSearch.do 는 막히고 .ax 만 된다.)
    """
    import requests

    ua = {"User-Agent": "Mozilla/5.0"}
    s = requests.Session()
    s.headers.update(ua)
    s.get("https://dart.fss.or.kr/dsab007/main.do", timeout=15)
    ymd = day.strftime("%Y%m%d")
    r = s.post("https://dart.fss.or.kr/dsab007/detailSearch.ax", timeout=15, data={
        "currentPage": 1, "maxResults": 100, "maxLinks": 10, "sort": "date", "series": "desc",
        "option": "report", "reportNamePopYn": "N", "reportName": "무상증자", "startDate": ymd,
        "endDate": ymd, "businessCode": "all", "corporationType": "all", "closingAccountsMonth": "all"})
    r.encoding = "utf-8"
    key = corp.replace(" ", "")
    rcps = []
    for tr in re.findall(r"<tr[\s\S]*?</tr>", r.text):
        rcp = re.search(r"rcpNo=(\d+)", tr)
        tds = re.findall(r"<td[\s\S]*?</td>", tr)
        name = re.search(r"openCorpInfoNew\('(\d+)'[\s\S]*?>\s*([^<]+?)\s*</a>", tr)
        if not rcp or len(tds) < 5 or not name:
            continue
        report = _strip(tds[2])
        if name.group(2).replace(" ", "") == key and "결정" in report and "종속회사" not in report:
            rcps.append(rcp.group(1))
    if not rcps:
        return {}
    rcp = max(rcps)
    m = s.get(f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp}", timeout=15).text
    texts = []
    for blk in re.findall(r"node1\['text'\][\s\S]*?node1\['dtd'\] = \"[^\"]*\"", m):
        kv = dict(re.findall(r"node1\['(\w+)'\] = \"([^\"]*)\"", blk))
        if "증자" not in kv.get("text", ""):
            continue
        body = s.get("https://dart.fss.or.kr/report/viewer.do", timeout=15, params={
            k: kv[k] for k in ("rcpNo", "dcmNo", "eleId", "offset", "length", "dtd") if k in kv})
        try:
            texts.append(_strip(body.content.decode("utf-8")))
        except UnicodeDecodeError:
            texts.append(_strip(body.content.decode("cp949", errors="replace")))
    terms = parse_terms("\n".join(texts))
    return {**terms, "rcp_no": rcp} if terms.get("record_date") else {}


def prev_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def next_weekday(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def ex_notice_of(row: dict) -> Optional[dict]:
    """KIND 목록 한 줄이 권리락 안내면 {code, at, title}. 유상증자 권리락은 뺀다."""
    title = row.get("title") or ""
    if "권리락" not in title or "유상증자" in title or re.match(r"^\[[^\]]*정정\]", title):
        return None
    code = row.get("code5") or ""
    if len(code) == 5:
        code += "0"
    if not re.fullmatch(r"\d{6}", code):
        return None
    try:
        at = datetime.strptime(row["time"].strip()[:16], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    except (KeyError, ValueError):
        return None
    return {"code": code, "at": at.isoformat(timespec="minutes"), "title": title}


class BonusWatcher:
    """전 계좌 공유 공시 감시. 새 무상증자 결정 공시를 각 계좌 ``bonus_trades``에 넘긴다."""

    POLL_SEC = float(os.getenv("BONUS_POLL_SEC") or 60)
    ACTIVE_FROM, ACTIVE_TO = time(7, 0), time(20, 0)
    # 장 시작 직후는 [상한가 매매]의 시간이다. 이 사이 공시는 조금 늦게 잡힌다(목표 시각엔 영향 없음).
    QUIET = (time(8, 59, 30), time(9, 1, 30))
    KEEP_EVENTS = 100
    EX_EVERY = 5                 # 권리락 안내('권리락' 검색)는 이 횟수에 한 번(유가 안내는 '무상증자'로 안 걸린다)
    EX_DAYS = 7
    TERMS_TRIES, TERMS_RETRY_SEC = 5, 120.0   # DART 본문이 늦게 올라오면 다시 본다

    def __init__(self, manager: "AccountManager",
                 fetch: Callable[..., List[dict]] = fetch_kind,
                 path: Optional[Path] = None,
                 terms: Callable[[str, date], dict] = fetch_terms) -> None:
        self.manager = manager
        self.fetch = fetch
        self.fetch_terms = terms
        self._polls = 0
        self._terms_tasks: Dict[str, asyncio.Task] = {}
        self._sim_ratio = 1.0  # (데모) 가짜 공시의 1주당 배정
        # 첫 계좌 상태 파일 옆(state.json → state.bonus_watch.json). 데모·미리보기 서버가 실서버의
        # '본 공시' 기록을 건드려 실서버가 공시를 놓치지 않게 인스턴스마다 따로 둔다.
        if path is None:
            env = os.getenv("BACH_STATE_BONUS_WATCH")
            hub = next(iter(manager.hubs.values()), None)
            path = Path(env) if env else (
                hub._state_path.with_suffix(".bonus_watch.json") if hub is not None
                else Path(__file__).resolve().parent.parent / "state.bonus_watch.json")
        self._path = path
        self.seen: Dict[str, str] = {}        # 접수번호 → 공시일(YYYY-MM-DD)
        self.events: List[dict] = []          # 최근 공시(새것이 뒤)
        self.status = {"source": "KIND", "enabled": os.getenv("BONUS_WATCH", "1") != "0",
                       "poll_sec": self.POLL_SEC, "last_poll": None, "ok": None, "error": ""}
        self._task: Optional[asyncio.Task] = None
        self._load()

    # -- 영속화 --------------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("공시 감시 상태 로드 실패(%s): %s", self._path, e)
            return
        self.seen = dict(raw.get("seen") or {})
        self.events = list(raw.get("events") or [])[-self.KEEP_EVENTS:]

    def _persist(self, today: Optional[date] = None) -> None:
        # 이레 넘은 접수번호는 잊는다(조회 범위는 직전 평일~오늘). 기준일은 조회한 날 — 실제 시계와
        # 따로 놀면 아직 조회 범위 안인 공시를 잊고 다시 처리한다(자동 매수 중복).
        cutoff = ((today or now_kst().date()) - timedelta(days=7)).isoformat()
        self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}
        try:
            self._path.write_text(json.dumps({"seen": self.seen, "events": self.events},
                                             ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.warning("공시 감시 상태 저장 실패(%s): %s", self._path, e)

    # -- 조회 ----------------------------------------------------------------
    def payload(self) -> dict:
        return {"status": self.status, "events": list(reversed(self.events))}

    def _broadcast(self) -> None:
        self.manager.broadcast({"type": "bonus_watch", **self.payload()})

    def _broadcast_status(self) -> None:
        # 공시 목록 없이 상태만(정상↔오류가 바뀔 때).
        self.manager.broadcast({"type": "bonus_watch_status", "status": self.status})

    @staticmethod
    def search_range(today: date) -> tuple:
        """직전 평일~오늘. 월요일이면 금요일부터라 주말 공시도 들어간다."""
        start = today - timedelta(days=1)
        while start.weekday() >= 5:
            start -= timedelta(days=1)
        return start, today

    def should_poll(self, now: datetime) -> bool:
        t = now.time()
        return (now.weekday() < 5 and self.ACTIVE_FROM <= t < self.ACTIVE_TO
                and not (self.QUIET[0] <= t < self.QUIET[1]))

    # -- 감시 ----------------------------------------------------------------
    def start(self) -> None:
        if self.status["enabled"] and self._task is None:
            self._task = asyncio.create_task(self._loop())
            # 권리락 일정을 아직 모르는 카드(재시작 전 DART 조회 실패 등)는 다시 읽는다.
            for hub in self.manager.hubs.values():
                for job in hub.bonus_trades.jobs_missing_terms():
                    self.want_terms(job.code, job.name, datetime.fromisoformat(job.disclosed_at).date())

    async def close(self) -> None:
        task, self._task = self._task, None
        tasks = [t for t in (task, *self._terms_tasks.values()) if t is not None]
        self._terms_tasks.clear()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # -- 권리락 일정 -----------------------------------------------------------
    def want_terms(self, code: str, corp: str, day: date) -> None:
        """DART 본문 조건(배정 비율·신주배정기준일·상장예정일)을 백그라운드로 읽어 각 계좌 카드에 넣는다."""
        t = self._terms_tasks.get(code)
        if t is None or t.done():
            self._terms_tasks[code] = asyncio.create_task(self._terms(code, corp, day))

    async def _terms(self, code: str, corp: str, day: date) -> None:
        for i in range(self.TERMS_TRIES):
            try:
                terms = await asyncio.to_thread(self.fetch_terms, corp, day)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                terms = {}
                logger.warning("[%s] DART 무상증자 조건 조회 실패(%d회): %s", code, i + 1, e)
            if terms:
                for hub in self.manager.hubs.values():
                    hub.bonus_trades.apply_terms(code, terms)
                return
            await asyncio.sleep(self.TERMS_RETRY_SEC)
        self.manager._log(f"[{code}] ⚠️ 무상증자 조건(배정 비율·신주배정기준일)을 DART에서 못 읽음 — "
                          "권리락일은 안내 공시로만 확정")
        for hub in self.manager.hubs.values():
            hub.bonus_trades.terms_failed(code)

    def _apply_ex(self, rows: List[dict]) -> None:
        for n in (ex_notice_of(r) for r in rows):
            if n is None:
                continue
            for hub in self.manager.hubs.values():
                hub.bonus_trades.apply_ex_notice(n)

    def _wants_ex(self) -> bool:
        return any(hub.bonus_trades.wants_ex_notice() for hub in self.manager.hubs.values())

    async def _poll_ex(self, now: datetime) -> None:
        try:
            rows = await asyncio.to_thread(self.fetch, now.date() - timedelta(days=self.EX_DAYS),
                                           now.date(), "권리락")
        except Exception as e:  # noqa: BLE001
            logger.warning("KIND 권리락 안내 조회 실패: %s", e)
            return
        self._apply_ex(rows)

    async def _loop(self) -> None:
        self.manager._log("📰 무상증자 공시 감시 시작 (KIND)")
        while True:
            try:
                if self.should_poll(now_kst()):
                    await self.poll_once()
                await asyncio.sleep(self.POLL_SEC)
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.exception("공시 감시 오류")
                await asyncio.sleep(self.POLL_SEC)

    async def poll_once(self, now: Optional[datetime] = None) -> List[Disclosure]:
        """한 번 조회해 새 공시를 처리한다. 매수 대상으로 넘긴 공시 목록을 돌려준다."""
        now = now or now_kst()
        try:
            rows = await asyncio.to_thread(self.fetch, *self.search_range(now.date()))
        except Exception as e:  # noqa: BLE001
            first = self.status["ok"] is not False
            self.status.update(last_poll=now.isoformat(timespec="seconds"), ok=False,
                               error=f"{type(e).__name__}: {e}"[:200])
            if first:  # 연속 실패는 한 번만 알린다(화면 방송도)
                self.manager._log(f"⚠️ 무상증자 공시 조회 실패: {self.status['error']}")
                self._broadcast_status()
            return []
        recovered = self.status["ok"] is False
        self.status.update(last_poll=now.isoformat(timespec="seconds"), ok=True, error="")
        if recovered:
            self.manager._log("무상증자 공시 조회 회복")
            self._broadcast_status()
        self._apply_ex(rows)  # 코스닥 '권리락(무상증자)'는 '무상증자' 검색에 같이 걸린다
        self._polls += 1
        if self._polls % self.EX_EVERY == 1 and self._wants_ex():
            await self._poll_ex(now)
        found = [d for d in (classify(r) for r in rows) if d is not None and d.acptno not in self.seen]
        dispatched = []
        for d in sorted(found, key=lambda x: x.disclosed_at):
            self.seen[d.acptno] = d.at.date().isoformat()
            self.events.append({**asdict(d), "detected_at": now.isoformat(timespec="seconds")})
            tag = "유무상" if d.paid_too else "무상"
            note = (" — 대상 아님: " + d.why) if not d.eligible else ""
            self.manager._log(f"📰 {tag}증자 공시: {d.corp}({d.code or '—'}) {d.at:%m-%d %H:%M} {d.title}{note}")
            if d.eligible:
                dispatched.append(d)
        if found:
            self.events = self.events[-self.KEEP_EVENTS:]
            self._persist(now.date())
        for d in dispatched:
            added = False
            for hub in self.manager.hubs.values():
                try:
                    job = await hub.bonus_trades.on_disclosure(d)
                    added = added or d.code in hub.bonus_trades.jobs
                    if job is not None and d.acptno.startswith("SIM"):
                        # 데모: DART 대신 가짜 배정 비율(매수 작업이 돌기 전에)
                        hub.bonus_trades.apply_terms(d.code, {"ratio": self._sim_ratio})
                except Exception:  # noqa: BLE001
                    logger.exception("[%s] 무상증자 매수 처리 오류(%s)", d.code, hub.account)
            if added and not d.acptno.startswith("SIM"):
                self.want_terms(d.code, d.corp, d.at.date())
        if found:
            self._broadcast()
        return dispatched

    async def simulate(self, code: str, name: str, title: str = "", paid_too: bool = False,
                       minutes_ago: float = 0.0, ratio: float = 1.0) -> Disclosure:
        """(데모) 가짜 공시를 감지한 것처럼 처리한다. ``ratio``: DART 본문 대신 쓸 1주당 배정."""
        at = (now_kst() - timedelta(minutes=minutes_ago)).replace(second=0, microsecond=0)
        title = title or ("유무상증자결정" if paid_too else "무상증자결정")
        row = {"time": at.strftime("%Y-%m-%d %H:%M"), "corp": name or code, "title": title,
               "acptno": f"SIM{_time.time_ns()}", "code5": code[:5] if code.endswith("0") else code,
               "mkt": "ko"}
        d = classify(row)
        if d is None:
            raise ValueError("제목에 '무상증자'·'결정'이 있어야 합니다.")
        d.code = code
        orig, self.fetch = self.fetch, (lambda _start, _end, keyword="무상증자":
                                        [row] if keyword == "무상증자" else [])
        try:
            self._sim_ratio = ratio
            await self.poll_once()
        finally:
            self.fetch = orig
        return d


# ---------------------------------------------------------------- 계좌별 매수
SETTINGS_VERSION = 2


@dataclass
class BonusSettings:
    enabled: bool = False            # 계좌별 자동 매수 스위치(기본 꺼짐)
    budget: int = 1_000_000          # 한 종목 매수 금액 상한
    order_mode: str = "expected"
    take_profit_pct: float = 20.0    # 평단 +% 이상 익절
    take_profit_partial: bool = False  # 익절: 전량(False) / 절반 매도 후 나머지 트레일링(True)
    trail_pct: float = 5.0           # 부분 익절 뒤 고점 대비 −%
    stop_pct: float = 15.0           # 평단 −% 이하 전량 매도
    skip_above_pct: float = 0.0      # >0: (장중 공시) 체결 기준가가 기준가 대비 이 % 이상이면 안 산다
    open_max_pct: float = 10.0       # 장전·장후 공시: 시가 단일가 예상가가 기준가 대비 이 %를 넘으면 안 산다
    include_paid: bool = False       # 유무상증자도 산다
    min_ratio: float = 1.0           # 1주당 배정이 이 주 이상일 때만 산다(DART 본문). 0이면 보지 않음
    lead_sec: int = 20               # expected: 단일가 마감 몇 초 전에 낼까
    resume_min: int = 40             # 장중 공시 → 첫 체결까지(정지 30분 + 재개 단일가 10분)

    def exits(self, enabled: bool = True) -> tuple:
        """새 카드의 (손절, 익절) 규칙."""
        return (ExitRule(enabled=enabled, mode="pct", pct=self.stop_pct),
                ExitRule(enabled=enabled, mode="pct", pct=self.take_profit_pct,
                         partial=self.take_profit_partial, trail_pct=self.trail_pct))

    @classmethod
    def parse(cls, data: dict, base: Optional["BonusSettings"] = None) -> "BonusSettings":
        cur = asdict(base or cls())
        for f in fields(cls):
            if f.name in (data or {}):
                cur[f.name] = data[f.name]
        try:
            s = cls(enabled=bool(cur["enabled"]), budget=int(float(cur["budget"])),
                    order_mode=str(cur["order_mode"]), take_profit_pct=float(cur["take_profit_pct"]),
                    take_profit_partial=bool(cur["take_profit_partial"]),
                    trail_pct=float(cur["trail_pct"]),
                    stop_pct=float(cur["stop_pct"]), skip_above_pct=float(cur["skip_above_pct"] or 0),
                    open_max_pct=float(cur["open_max_pct"] or 0),
                    include_paid=bool(cur["include_paid"]), min_ratio=float(cur["min_ratio"] or 0),
                    lead_sec=int(float(cur["lead_sec"])),
                    resume_min=int(float(cur["resume_min"])))
        except (TypeError, ValueError):
            raise ValueError("설정 값은 숫자여야 합니다.") from None
        if s.order_mode not in ORDER_MODES:
            raise ValueError(f"주문 방식은 {', '.join(ORDER_MODES)} 중 하나여야 합니다.")
        if not 10_000 <= s.budget <= 100_000_000:
            raise ValueError("매수 금액은 1만 원 이상 1억 원 이하여야 합니다.")
        if not 0 < s.take_profit_pct < 1000:
            raise ValueError("익절 비율은 0 초과 1000% 미만이어야 합니다.")
        if not 0 < s.stop_pct < 100:
            raise ValueError("손절 비율은 0 초과 100% 미만이어야 합니다.")
        if not 0 < s.trail_pct < 50:
            raise ValueError("트레일링 비율은 0 초과 50% 미만이어야 합니다.")
        if not 0 <= s.skip_above_pct <= 30:
            raise ValueError("건너뛰기 기준은 0(끔)~30% 여야 합니다.")
        if not 0 <= s.open_max_pct <= 30:
            raise ValueError("장전 공시 시가 상한은 0(제한 없음)~30% 여야 합니다.")
        if not 0 <= s.min_ratio <= 10:
            raise ValueError("1주당 배정 기준은 0(보지 않음)~10주여야 합니다.")
        if not 3 <= s.lead_sec <= 120:
            raise ValueError("마감 전 주문 시각은 3~120초여야 합니다.")
        if not 1 <= s.resume_min <= 120:
            raise ValueError("재개까지 시간은 1~120분이어야 합니다.")
        return s


GAP_DEFAULT_STOP, GAP_DEFAULT_TP = 3.0, 10.0


@dataclass
class GapPlan:
    """갭상승 모드: 켠 날(D) 다음 거래일부터 매일 시가가 전일 기준가 × (1 + g%) 이상이면 시장가 매수."""
    budget: int = 1_000_000
    gap_pct: float = 1.0
    bull_open: bool = False          # 전일이 양봉이면 전일 시가를 기준으로
    stop: dict = field(default_factory=lambda: ExitRule(True, "pct", GAP_DEFAULT_STOP).to_dict())
    take_profit: dict = field(default_factory=lambda: ExitRule(True, "pct", GAP_DEFAULT_TP).to_dict())
    started: str = ""                # 켠 날 YYYY-MM-DD
    checked: str = ""                # 마지막으로 판정한 날
    message: str = ""

    @classmethod
    def parse(cls, data: dict, started: str) -> "GapPlan":
        data = data or {}
        try:
            g = cls(budget=int(float(data.get("budget", 1_000_000))),
                    gap_pct=float(data.get("gap_pct", 1.0)), bull_open=bool(data.get("bull_open")),
                    started=started)
        except (TypeError, ValueError):
            raise ValueError("갭상승 설정 값은 숫자여야 합니다.") from None
        if not 10_000 <= g.budget <= 100_000_000:
            raise ValueError("매수 금액은 1만 원 이상 1억 원 이하여야 합니다.")
        if not 0 <= g.gap_pct <= 29:
            raise ValueError("갭 비율은 0~29% 여야 합니다.")
        stop = ExitRule.parse({"enabled": True, **(data.get("stop") or {})}, "stop", GAP_DEFAULT_STOP)
        tp = ExitRule.parse({"enabled": True, **(data.get("take_profit") or {})}, "take_profit", GAP_DEFAULT_TP)
        if stop.mode == "price" and tp.mode == "price" and stop.price >= tp.price:
            raise ValueError("손절 라인은 익절 라인보다 낮아야 합니다.")
        g.stop, g.take_profit = stop.to_dict(), tp.to_dict()
        return g

    def rules(self) -> tuple:
        return (ExitRule(**self.stop), ExitRule(**self.take_profit))

    def describe(self) -> str:
        stop, tp = self.rules()
        def one(r: ExitRule, side: str) -> str:
            if r.mode == "price":
                return f"{r.price:,.0f}원"
            return f"{'−' if side == 'stop' else '+'}{r.pct:g}%"
        part = f"(절반, 나머지 고점 −{tp.trail_pct:g}%)" if tp.partial else ""
        return (f"{self.budget:,}원, 시가 ≥ 전일 {'종가(양봉이면 시가)' if self.bull_open else '종가'} "
                f"+{self.gap_pct:g}%, 익절 {one(tp, 'take_profit')}{part}, 손절 {one(stop, 'stop')}")


@dataclass
class BuyJob:
    code: str
    name: str
    acptno: str
    disclosed_at: str
    title: str
    mode: str
    budget: int
    target_at: str                   # 첫 체결(단일가 마감) 예상 시각
    status: str = "waiting"          # waiting / ordering / sent / skipped / failed / cancelled
    message: str = ""
    order_no: str = ""
    qty: int = 0
    price: float = 0.0               # 주문 가격(시장가는 수량 기준가)
    basis: str = ""                  # 수량을 정한 가격의 출처
    attempts: int = 0
    created_at: str = ""
    updated_at: str = ""
    kind: str = "intraday"           # intraday: 장중 재개 단일가 / open: 장 시작(09:00) 단일가
    # 권리락 일정(DART 본문 + KIND 안내). ex_date 는 기준일 직전 평일(예정) → 안내 공시로 확정.
    ratio: float = 0.0
    terms: str = ""                  # DART 본문 조건: "" 조회 중 / ok 읽음 / failed 못 읽음
    record_date: str = ""
    listing_date: str = ""
    ex_date: str = ""
    ex_confirmed: bool = False
    ex_notice_at: str = ""
    ex_notice_title: str = ""
    gap: Optional[dict] = None       # 켜진 갭상승 계획(GapPlan)
    task: Optional[asyncio.Task] = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict:
        # asdict 는 Task 를 깊은 복사하려다 실패한다.
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "task"}


def target_time(disclosed: datetime, resume_min: int) -> datetime:
    """공시 뒤 첫 체결(단일가 마감)이 날 것으로 보는 시각."""
    t = disclosed.time()
    if disclosed.weekday() < 5:
        if t < REGULAR_OPEN:
            return disclosed.replace(hour=9, minute=0, second=0, microsecond=0)
        if t < REGULAR_CLOSE:
            resume = disclosed + timedelta(minutes=resume_min)
            if resume.time() <= LAST_RESUME and resume.date() == disclosed.date():
                return resume
    d = disclosed.date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return datetime.combine(d, REGULAR_OPEN, tzinfo=KST)


class _Skip(Exception):
    pass


class BonusTradeManager:
    ULC_YIELD_MAX_SEC = 90.0  # 대금 양봉 1차와 같은 상한
    RETRY_SEC = 15.0
    STEP_SEC = 1.0           # 대기 루프 간격(테스트는 줄인다)
    SILENCE_SEC = 180.0      # 이만큼 체결이 없다가 새 체결이 오면 '재개'로 본다

    GAP_STEP_SEC = 5.0       # 갭상승 판정 루프 간격
    GAP_GIVE_UP = time(9, 10)  # 이때까지 오늘 시가를 못 얻으면 그날은 넘긴다

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.settings = BonusSettings()
        self.jobs: Dict[str, BuyJob] = {}
        self._gap_tasks: Dict[str, asyncio.Task] = {}
        self._path: Path = hub._state_path.with_suffix(".bonus.json")
        self._now: Callable[[], datetime] = now_kst

    # -- 조회·영속화 ---------------------------------------------------------
    def payload(self) -> dict:
        jobs = sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)
        return {"settings": asdict(self.settings), "jobs": [j.to_dict() for j in jobs]}

    def broadcast(self) -> None:
        self.hub.broadcast({"type": "bonus", **self.payload()})

    def persist(self) -> None:
        data = {"version": SETTINGS_VERSION, "settings": asdict(self.settings),
                "jobs": [j.to_dict() for j in self.jobs.values()]}
        try:
            self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.warning("무상증자 매매 상태 저장 실패(%s): %s", self._path, e)

    def restore(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as e:  # noqa: BLE001
            self.hub._log(f"⚠️ 무상증자 매매 상태 파일을 읽지 못했습니다: {self._path.name} ({e})")
            return
        saved = dict(raw.get("settings") or {})
        if raw.get("version") is None and (saved.get("take_profit_pct"), saved.get("stop_pct")) == (25.0, 10.0):
            # 예전 기본값(익절 +25%·손절 −10%) 그대로였으면 새 기본값(+20%·−15%)으로 옮긴다.
            saved.update(take_profit_pct=20.0, stop_pct=15.0)
        try:
            self.settings = BonusSettings.parse(saved)
        except ValueError as e:
            self.hub._log(f"⚠️ 저장된 무상증자 매매 설정 오류 — 기본값(자동 매수 꺼짐): {e}")
        names = {f.name for f in fields(BuyJob)} - {"task"}
        for entry in raw.get("jobs") or []:
            try:
                job = BuyJob(**{k: v for k, v in entry.items() if k in names})
            except TypeError:
                continue
            self.jobs[job.code] = job
            if job.status in ACTIVE_JOB:
                self._log(job, f"재시작 — 매수 대기 이어감({job.mode})")
                self._start(job)
            if job.gap:
                self._start_gap(job.code)

    def _log(self, job: BuyJob, text: str, **extra) -> None:
        self.hub._log(f"[{job.code}] 무상증자 매매: {text}",
                      event="bonus_trade", code=job.code, status=job.status, mode=job.mode, **extra)

    def _update(self, job: BuyJob, status: Optional[str] = None, message: Optional[str] = None,
                **values) -> None:
        if status:
            job.status = status
        if message is not None:
            job.message = message
        for k, v in values.items():
            setattr(job, k, v)
        job.updated_at = self._now().isoformat(timespec="seconds")
        self.persist()
        self.broadcast()

    # -- 설정 ----------------------------------------------------------------
    def set_settings(self, data: dict) -> dict:
        new = BonusSettings.parse(data, self.settings)
        was = self.settings.enabled
        self.settings = new
        self.persist()
        onoff = "" if was == new.enabled else (" — 자동 매수 켬" if new.enabled else " — 자동 매수 끔")
        self.hub._log(f"무상증자 매매 설정 저장{onoff}: {self.describe()}", event="bonus_settings",
                      settings=asdict(new))
        self.broadcast()
        return self.payload()

    def describe(self) -> str:
        s = self.settings
        mode = {"expected": "예상 체결가", "upper_limit": "상한가 지정가", "market": "시장가",
                "after_resume": "재개 후 첫 체결가"}[s.order_mode]
        skip = f", 재개가 기준가 +{s.skip_above_pct:g}% 이상 건너뜀" if s.skip_above_pct > 0 else ""
        opening = f", 시가 매수는 +{s.open_max_pct:g}% 이하만" if s.open_max_pct > 0 else ""
        part = f"(절반, 나머지 고점 −{s.trail_pct:g}%)" if s.take_profit_partial else ""
        ratio = f", 1주당 배정 {s.min_ratio:g}주 이상만" if s.min_ratio > 0 else ""
        return (f"{s.budget:,}원 {mode}, 익절 +{s.take_profit_pct:g}%{part}, 손절 −{s.stop_pct:g}%"
                f"{skip}{opening}{ratio}" + (", 유무상 포함" if s.include_paid else ""))

    # -- 공시 → 매수 작업 -----------------------------------------------------
    async def on_disclosure(self, d: Disclosure) -> Optional[BuyJob]:
        """공시 → 카드(수동 매매 목록 origin "bonus") + 매수 작업. 사지 않을 공시도 '수동' 카드로 올린다."""
        s = self.settings
        hub = self.hub
        label = f"[{d.code}] 무상증자 공시({d.corp})"
        if not s.enabled:
            hub._log(f"{label}: 이 계좌는 자동 매수가 꺼져 있어 건너뜀")
            return None
        prev = self.jobs.get(d.code)
        if prev is not None and prev.status in ACTIVE_JOB:
            hub._log(f"{label}: 이미 매수 대기 중이라 건너뜀")
            return None
        if d.code in hub.manual_trades.items:
            hub._log(f"{label}: 이미 수동 매매 목록에 있어 건너뜀")
            return None
        now = self._now()
        target = self._target(d.at)
        kind = "open" if not self._demo() and target.time() == REGULAR_OPEN else "intraday"
        skip = ""
        if d.paid_too and not s.include_paid:
            skip = "유무상증자(설정에서 켤 수 있음)"
        elif now >= target:
            # 서버가 꺼져 있던 동안 이미 재개돼 가격이 움직였다 — 공시 직후 감지와 같은 매수가 아니다.
            skip = f"감지({now:%m-%d %H:%M}) 때 첫 체결 예상 시각이 이미 지남"
        name = d.corp  # KIND 회사명 — 키움 종목명 조회(REST)를 아낀다
        try:
            # 공시 제목은 매수 작업(job)에 둔다 — 수동 매매 notice 는 '확인할 알림'(칩 ⚠)이다.
            hub.manual_trades.add(d.code, name or d.corp, "bonus", exits=s.exits(enabled=not skip))
        except ValueError as e:
            hub._log(f"{label}: 건너뜀 — {e}")
            return None
        stamp = now.isoformat(timespec="seconds")
        job = BuyJob(code=d.code, name=name or d.corp, acptno=d.acptno, disclosed_at=d.disclosed_at,
                     title=d.title, mode=s.order_mode, budget=s.budget,
                     target_at=target.isoformat(timespec="seconds"), kind=kind,
                     created_at=stamp, updated_at=stamp)
        self._stop_gap(d.code)
        self.jobs[d.code] = job
        if skip:
            job.status, job.message = "skipped", skip
            self.persist()
            self._log(job, f"매수 안 함 — {skip} · 수동 카드로 올림")
            self.broadcast()
            return None
        self.persist()
        when = "시가 단일가" if kind == "open" else "첫 체결 예상"
        self._log(job, f"매수 대기 시작 — {self.describe()}, {when} {job.target_at[11:16]}"
                  + ("" if job.target_at[:10] == now.date().isoformat() else f"({job.target_at[:10]})"))
        self._start(job)
        self.broadcast()
        return job

    def cancel(self, code: str) -> dict:
        job = self.jobs.get(code)
        if job is None:
            raise KeyError(code)
        if job.status not in ACTIVE_JOB:
            raise ValueError("대기 중인 매수가 아닙니다.")
        if job.task is not None and job.task is not asyncio.current_task():
            job.task.cancel()
        job.task = None
        self._update(job, "cancelled", "사람이 취소")
        self._log(job, "매수 대기 취소 — 수동 카드로")
        self._manual_exits(job.code, "매수 대기 취소")
        return self.payload()

    def _start(self, job: BuyJob) -> None:
        if job.task is None or job.task.done():
            job.task = asyncio.create_task(self._run(job))

    async def close(self) -> None:
        tasks = [j.task for j in self.jobs.values() if j.task is not None]
        tasks += list(self._gap_tasks.values())
        self._gap_tasks.clear()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # -- 카드 상태(자동·수동·갭상승)·옮기기 --------------------------------------
    def _manual_exits(self, code: str, why: str) -> None:
        """손절·익절을 끈 '수동' 상태로(설정값은 남겨 두어 다시 켤 때 보인다)."""
        it = self.hub.manual_trades.items.get(code)
        if it is None or not (it.stop.enabled or it.take_profit.enabled):
            return
        stop, tp = replace(it.stop, enabled=False), replace(it.take_profit, enabled=False)
        self.hub.manual_trades.replace_exits(code, stop, tp, f"끔({why})")

    def _card(self, code: str) -> BuyJob:
        job = self.jobs.get(code)
        if job is None or self.hub.manual_trades.items.get(code) is None \
                or self.hub.manual_trades.items[code].origin != "bonus":
            raise KeyError(code)
        return job

    def to_manual(self, code: str) -> dict:
        """[수동으로]: 매수 대기·갭상승을 멈추고 자동 손절·익절을 끈다. 카드는 무상증자 화면에 남는다."""
        job = self._card(code)
        if job.status in ACTIVE_JOB:
            if job.task is not None and job.task is not asyncio.current_task():
                job.task.cancel()
            job.task = None
            self._update(job, "cancelled", "수동 전환")
        if job.gap:
            self._stop_gap(code)
            job.gap = None
            self._update(job)
        self._manual_exits(code, "수동 전환")
        self._log(job, "수동 전환 — 자동 매수·손절·익절 멈춤")
        self.broadcast()
        return self.payload()

    def set_gap(self, code: str, data: dict) -> dict:
        """갭상승 모드 켜기(수동 상태에서만). 켠 날 다음 거래일부터 매일 장 시작에 판정한다."""
        job = self._card(code)
        it = self.hub.manual_trades.items[code]
        if job.status in ACTIVE_JOB:
            raise ValueError("매수 대기 중입니다. [수동으로] 바꾼 뒤 켜세요.")
        if it.stop.enabled or it.take_profit.enabled:
            raise ValueError("손절·익절을 감시 중입니다. [수동으로] 바꾼 뒤 켜세요.")
        plan = GapPlan.parse(data, self._now().date().isoformat())
        self._update(job, gap=asdict(plan))
        self._log(job, f"갭상승 모드 켬 — {plan.describe()} · 다음 거래일 시가부터 매일 판정")
        self._start_gap(code)
        return self.payload()

    def clear_gap(self, code: str) -> dict:
        job = self._card(code)
        if not job.gap:
            raise ValueError("갭상승 모드가 꺼져 있습니다.")
        self._stop_gap(code)
        self._update(job, gap=None)
        self._log(job, "갭상승 모드 끔 — 수동")
        return self.payload()

    async def move_to_manual_page(self, code: str) -> dict:
        """[수동 매매 화면으로]: 카드를 수동 매매 목록(origin "from_bonus")으로 넘긴다. 손절·익절은 그대로."""
        job = self._card(code)
        self.to_manual_quiet(job)
        mt = self.hub.manual_trades
        mt.items[code].origin = "from_bonus"
        mt.persist()
        self._log(job, "수동 매매 화면으로 옮김")
        mt.broadcast(code)
        self.broadcast()
        return self.payload()

    async def move_to_zone(self, code: str) -> dict:
        """[구간 매매로]: 보유 0일 때만. 수동 매매 목록에서 빼고 구간 매매 '설정 중'으로 올린다."""
        job = self._card(code)
        name = self.hub.manual_trades.items[code].name
        self.hub.broker.invalidate()
        try:
            held = (await asyncio.to_thread(self.hub.broker.position, code)).quantity
        except Exception:  # noqa: BLE001
            raise ValueError("계좌 잔고를 확인하지 못했습니다. 잠시 후 다시 시도하세요.") from None
        if held > 0:
            raise ValueError(f"계좌에 {held}주를 보유 중입니다. 구간 매매로는 보유 0일 때만 옮길 수 있습니다 — "
                             "팔거나 [수동 매매 화면으로] 옮기세요.")
        self.to_manual_quiet(job)
        await self.hub.manual_trades.remove(code)
        self.hub.candle_trades.add_zone(code, name)
        self._log(job, "구간 매매로 옮김(설정 중)")
        self.broadcast()
        return self.payload()

    def to_manual_quiet(self, job: BuyJob) -> None:
        """옮기기 전에 이 카드가 돌리던 작업(매수 대기·갭상승)만 멈춘다."""
        if job.status in ACTIVE_JOB:
            if job.task is not None and job.task is not asyncio.current_task():
                job.task.cancel()
            job.task = None
            self._update(job, "cancelled", "화면에서 옮김")
        if job.gap:
            self._stop_gap(job.code)
            self._update(job, gap=None)

    # -- 권리락 일정 -----------------------------------------------------------
    def jobs_missing_terms(self) -> List[BuyJob]:
        cutoff = (self._now() - timedelta(days=90)).isoformat()
        return [j for j in self.jobs.values()
                if not j.record_date and j.disclosed_at >= cutoff and not j.acptno.startswith("SIM")]

    def wants_ex_notice(self) -> bool:
        today = self._now().date().isoformat()
        return any(not j.ex_confirmed and (not j.ex_date or j.ex_date >= today)
                   for j in self.jobs.values() if not j.acptno.startswith("SIM"))

    def apply_terms(self, code: str, terms: dict) -> None:
        job = self.jobs.get(code)
        if job is None:
            return
        record = terms.get("record_date") or ""
        values = {"ratio": float(terms.get("ratio") or 0), "record_date": record,
                  "listing_date": terms.get("listing_date") or "", "terms": "ok"}
        if record and not job.ex_confirmed:
            values["ex_date"] = prev_weekday(date.fromisoformat(record)).isoformat()
        self._update(job, **values)
        self._log(job, f"무상증자 조건 — 1주당 {values['ratio']:g}주, 신주배정기준일 {record}"
                  + (f", 권리락일 {job.ex_date}{'' if job.ex_confirmed else '(예정)'}" if job.ex_date else "")
                  + (f", 상장예정 {values['listing_date']}" if values["listing_date"] else ""))
        self._check_ratio_now(job)

    def terms_failed(self, code: str) -> None:
        """DART 본문을 끝내 못 읽음 — 배정 기준이 켜져 있으면 대기 중인 매수를 그만둔다."""
        job = self.jobs.get(code)
        if job is None or job.terms:
            return
        self._update(job, terms="failed")
        self._check_ratio_now(job)

    def _ratio_ok(self, job: BuyJob) -> bool:
        """주문해도 되는가. 배정 비율을 아직 모르면 False(기다림), 기준 미달·확인 불가면 _Skip."""
        need = self.settings.min_ratio
        if need <= 0 or job.ratio >= need:
            return True
        if job.ratio > 0:
            raise _Skip(f"1주당 배정 {job.ratio:g}주 < 기준 {need:g}주")
        if job.terms:
            raise _Skip(f"1주당 배정을 DART 본문에서 확인하지 못함(기준 {need:g}주)")
        return False

    def _check_ratio_now(self, job: BuyJob) -> None:
        """조건을 읽은 즉시: 대기 중인 매수가 기준에 못 미치면 주문 시각을 기다리지 않고 그만둔다."""
        if job.status not in ACTIVE_JOB or job.status == "ordering":
            return
        try:
            self._ratio_ok(job)
        except _Skip as e:
            if job.task is not None and job.task is not asyncio.current_task():
                job.task.cancel()
            job.task = None
            self._update(job, "skipped", str(e))
            self._log(job, f"매수 안 함 — {e} · 수동 카드로")
            self._manual_exits(job.code, "매수 안 함")

    def apply_ex_notice(self, n: dict) -> None:
        """KIND 권리락 안내 → 권리락일 확정(안내 다음 평일; 장 시작 전 안내면 그날)."""
        job = self.jobs.get(n["code"])
        if job is None or job.ex_notice_at == n["at"] or n["at"] < job.disclosed_at[:16]:
            return
        at = datetime.fromisoformat(n["at"])
        ex = at.date() if at.time() < REGULAR_OPEN else next_weekday(at.date())
        self._update(job, ex_notice_at=n["at"], ex_notice_title=n["title"], ex_date=ex.isoformat(),
                     ex_confirmed=True)
        self._log(job, f"권리락 안내({n['title']}, {at:%m-%d %H:%M}) — 권리락일 {ex.isoformat()}")

    # -- 갭상승 판정 -------------------------------------------------------------
    def _start_gap(self, code: str) -> None:
        t = self._gap_tasks.get(code)
        if t is None or t.done():
            self._gap_tasks[code] = asyncio.create_task(self._gap_loop(code))

    def _stop_gap(self, code: str) -> None:
        t = self._gap_tasks.pop(code, None)
        if t is not None and t is not asyncio.current_task():
            t.cancel()

    async def _gap_loop(self, code: str) -> None:
        warned = False
        while True:
            job = self.jobs.get(code)
            if job is None or not job.gap:
                return
            try:
                await self._gap_check(job)
                warned = False
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if not warned:
                    logger.exception("[%s] 갭상승 판정 오류", code)
                    self._log(job, f"⚠️ 갭상승 판정 오류: {type(e).__name__} — 다시 시도")
                    warned = True
            if not job.gap:
                return
            await asyncio.sleep(self.GAP_STEP_SEC)

    async def _gap_check(self, job: BuyJob) -> None:
        g = GapPlan(**job.gap)
        now = self._now()
        today = now.date().isoformat()
        if not self.hub.clock.is_open() or g.checked == today:
            return
        if not self._demo() and (now.weekday() >= 5 or now.time() < REGULAR_OPEN or today <= g.started):
            return  # 켠 날(D) 다음 거래일 장 시작부터
        await self._yield_to_ulc(job, deadline=False)
        bars = await asyncio.to_thread(self.hub.data.get_bars, job.code, DAY_INTERVAL)
        day = lambda b: datetime.fromtimestamp(b.time, tz=timezone.utc).date().isoformat()  # noqa: E731
        cur = next((b for b in reversed(bars) if day(b) == today), None)
        prev = next((b for b in reversed(bars) if day(b) < today), None)
        open_ = cur.open if cur is not None else 0.0
        if open_ <= 0:
            tick = self.hub.data.last_tick(job.code)
            open_ = tick.open if tick is not None and tick.open > 0 else 0.0
        if prev is None or open_ <= 0:
            if self._demo() or now.time() < self.GAP_GIVE_UP:
                return  # 시가가 아직 안 잡혔다 — 다음 루프에
            g.checked, g.message = today, f"{today[5:]} 시가·전일 봉을 못 얻어 넘김"
            self._update(job, gap=asdict(g))
            self._log(job, f"갭상승: {g.message}")
            return
        bull = prev.close > prev.open
        ref, what = ((prev.open, "전일(양봉) 시가") if g.bull_open and bull else (prev.close, "전일 종가"))
        need = ref * (1 + g.gap_pct / 100)
        basis = f"시가 {open_:,.0f} / {what} {ref:,.0f} +{g.gap_pct:g}% = {need:,.0f}"
        if open_ < need:
            g.checked, g.message = today, f"{today[5:]} 미달 — {basis}"
            self._update(job, gap=asdict(g))
            self._log(job, f"갭상승 미달 — {basis} · 다음 거래일에 다시")
            return
        result = await asyncio.to_thread(self.hub.broker.buy, job.code, g.budget)
        if not result.ok:
            g.message = f"{today[5:]} 매수 주문 실패 — {result.message} (다시 시도)"
            self._update(job, gap=asdict(g))
            self._log(job, f"⚠️ 갭상승 매수 주문 실패: {result.message} — 다시 시도")
            return
        self.hub.tag_order(result, "bonus", "갭상승 매수",
                           {"open": open_, "ref": ref, "ref_kind": what, "gap_pct": g.gap_pct,
                            "budget": g.budget, "acptno": job.acptno})
        self._stop_gap(job.code)
        self._update(job, gap=None, message=f"갭상승 매수 {today[5:]} — {basis}")
        stop, tp = g.rules()
        self.hub.manual_trades.replace_exits(job.code, stop, tp, "갭상승 매수 뒤 감시")
        self._log(job, f"갭상승 매수 주문 전송 — {result.message} ({basis}) · 손절·익절 감시 시작",
                  order_no=result.order_no)
        self.hub.broker.invalidate()
        await self.hub.manual_trades.broadcast_async(job.code)
        await self.hub.refresh_orders()

    # -- 매수 실행 -----------------------------------------------------------
    def _demo(self) -> bool:
        """수동 장 시계(mock 데모)면 실제 요일·장 시간을 따지지 않는다."""
        return not self.hub.clock.auto

    def _target(self, disclosed: datetime) -> datetime:
        if self._demo():
            return disclosed + timedelta(minutes=self.settings.resume_min)
        return target_time(disclosed, self.settings.resume_min)

    def _deadline(self, job: BuyJob) -> datetime:
        target = datetime.fromisoformat(job.target_at)
        if self._demo():
            return target + timedelta(days=1)
        return datetime.combine(target.date(), REGULAR_CLOSE, tzinfo=KST)

    def _accepting(self, now: datetime) -> bool:
        if self._demo():
            return True
        return now.weekday() < 5 and ACCEPT_FROM <= now.time() < REGULAR_CLOSE

    async def _run(self, job: BuyJob) -> None:
        try:
            if job.status == "ordering" and await self._already_ordered(job):
                return
            mode = job.mode
            if job.kind == "open" and mode != "after_resume":
                # 시가 단일가: 예상가로 상한(open_max_pct)을 확인한 뒤 낸다 — 시장가·상한가도 예상가 확인 후.
                await self._run_expected(job)
            elif mode in ("market", "upper_limit"):
                await self._run_now(job)
            elif mode == "expected":
                await self._run_expected(job)
            else:
                await self._run_after_resume(job)
        except asyncio.CancelledError:
            raise
        except _Skip as e:
            self._update(job, "skipped", str(e))
            self._log(job, f"매수 안 함 — {e} · 수동 카드로")
            self._manual_exits(job.code, "매수 안 함")
        except TimeoutError as e:
            self._update(job, "failed", str(e))
            self._log(job, f"⚠️ 매수 못 함 — {e} · 수동 카드로")
            self._manual_exits(job.code, "매수 못 함")
        except Exception as e:  # noqa: BLE001
            logger.exception("[%s] 무상증자 매수 작업 오류", job.code)
            self._update(job, "failed", f"오류: {type(e).__name__}")
            self._log(job, f"⚠️ 매수 작업 오류: {type(e).__name__} — 로그 확인 필요")
            self._manual_exits(job.code, "매수 작업 오류")

    def _check_deadline(self, job: BuyJob) -> None:
        if self._now() >= self._deadline(job):
            raise TimeoutError(f"기한({job.target_at[:10]} 15:30)까지 주문하지 못함")

    async def _wait_accepting(self, job: BuyJob) -> None:
        while not self._accepting(self._now()):
            self._check_deadline(job)
            await asyncio.sleep(min(30.0, self.STEP_SEC * 30))

    async def _run_now(self, job: BuyJob) -> None:
        """market / upper_limit: 감지 즉시(접수 시간이면) 주문, 거부되면 재시도."""
        while True:
            self._check_deadline(job)
            await self._wait_accepting(job)
            if await self._place(job):
                return
            await asyncio.sleep(self.RETRY_SEC)

    async def _run_expected(self, job: BuyJob) -> None:
        target = datetime.fromisoformat(job.target_at)
        order_at = target - timedelta(seconds=self.settings.lead_sec)
        watch = _ResumeWatch(self, job, target)
        zero_logged = False
        while self._now() < order_at:
            self._check_deadline(job)
            if watch.resumed():
                self._log(job, "예상보다 먼저 체결이 났다 — 첫 체결가 기준으로 전환")
                return await self._buy_after_resume(job)
            await asyncio.sleep(self.STEP_SEC)
        while True:
            self._check_deadline(job)
            if watch.resumed():
                self._log(job, "예상체결가로 주문하기 전에 체결이 났다 — 첫 체결가 기준으로 전환")
                return await self._buy_after_resume(job)
            quote = await self._quote(job)
            exp = (quote or {}).get("expected") or 0
            if exp > 0 and (quote or {}).get("upper", 0) > 0:
                self._skip_check(job, exp, quote.get("base") or 0, "예상체결가")
                if await self._place(job, quote=quote, basis_price=exp, basis="예상체결가"):
                    return
                await asyncio.sleep(self.RETRY_SEC)
            else:
                if not zero_logged:
                    zero_logged = True
                    self._log(job, "예상체결가·상한가를 아직 못 받음 — 다시 조회(응답 원문 기록)",
                              quote_raw=(quote or {}).get("raw"))
                await asyncio.sleep(max(self.STEP_SEC, 2 * self.STEP_SEC))

    async def _run_after_resume(self, job: BuyJob) -> None:
        target = datetime.fromisoformat(job.target_at)
        watch = _ResumeWatch(self, job, target)
        while not watch.resumed():
            self._check_deadline(job)
            await asyncio.sleep(self.STEP_SEC)
        await self._buy_after_resume(job)

    async def _buy_after_resume(self, job: BuyJob) -> None:
        tick = self.hub.data.last_tick(job.code)
        quote = await self._quote(job)
        base = (quote or {}).get("base") or 0
        if tick is not None:
            self._skip_check(job, tick.price, base, "첫 체결가")
        job.mode = "after_resume"
        while True:
            self._check_deadline(job)
            if await self._place(job):
                return
            await asyncio.sleep(self.RETRY_SEC)

    def _ulc_pending(self) -> bool:
        # REST 게이트는 프로세스 전역이라 다른 계좌의 상한가 1차 주문에도 양보한다.
        hubs = getattr(self.hub.manager, "hubs", None) or {self.hub.account: self.hub}
        return any(h.ulc_first_orders_pending() for h in hubs.values())

    async def _yield_to_ulc(self, job: BuyJob, deadline: bool = True) -> None:
        """키움 호출(조회·주문) 전에: 상한가 1차 주문이 남아 있으면 먼저 보내게 기다린다."""
        if not self._ulc_pending():
            return
        self._log(job, f"상한가 매매 1차 주문을 먼저 보냅니다 — 키움 호출 대기(최대 {self.ULC_YIELD_MAX_SEC:g}초)")
        started = _time.monotonic()
        while self._ulc_pending() and _time.monotonic() - started < self.ULC_YIELD_MAX_SEC:
            if deadline:
                self._check_deadline(job)
            await asyncio.sleep(self.STEP_SEC)

    def _skip_check(self, job: BuyJob, price: float, base: float, what: str) -> None:
        if job.kind == "open":
            # 장전·장후 공시: 시가가 기준가 +a%를 '넘지 않으면' 산다.
            pct = self.settings.open_max_pct
            if pct > 0 and base > 0 and price > base * (1 + pct / 100):
                raise _Skip(f"시가 {what} {price:,.0f}원이 기준가 {base:,.0f}원 대비 "
                            f"+{(price / base - 1) * 100:.1f}% (상한 +{pct:g}%)")
            return
        pct = self.settings.skip_above_pct
        if pct > 0 and base > 0 and price >= base * (1 + pct / 100):
            raise _Skip(f"{what} {price:,.0f}원이 기준가 {base:,.0f}원 대비 "
                        f"+{(price / base - 1) * 100:.1f}% (기준 +{pct:g}%)")

    async def _quote(self, job: BuyJob) -> Optional[dict]:
        await self._yield_to_ulc(job)
        try:
            return await asyncio.to_thread(self.hub.data.auction_quote, job.code)
        except Exception:  # noqa: BLE001
            return None

    async def _already_ordered(self, job: BuyJob) -> bool:
        """앞선 주문 결과를 모를 때(요청 오류·재시작) 계좌에 보유·미체결 매수가 있는지."""
        await self._yield_to_ulc(job)
        broker = self.hub.broker
        broker.invalidate()
        try:
            pos = await asyncio.to_thread(broker.position, job.code)
            unfilled = await asyncio.to_thread(broker.unfilled_orders)
        except Exception:  # noqa: BLE001
            return False
        buys = [o for o in unfilled.get(job.code, []) if o.get("side", "buy") == "buy"]
        if pos.quantity > 0 or buys:
            self._update(job, "sent", f"계좌에 보유 {pos.quantity}주·미체결 매수 {len(buys)}건이 있어 주문된 것으로 봄")
            self._log(job, job.message)
            return True
        return False

    async def _place(self, job: BuyJob, quote: Optional[dict] = None, basis_price: float = 0.0,
                     basis: str = "") -> bool:
        """주문 1회. 성공이면 True. 결과를 모르는 실패면 다음 시도 전에 계좌를 확인한다."""
        if not self._ratio_ok(job):
            if not job.message.startswith("1주당 배정 확인 대기"):
                self._log(job, "1주당 배정 확인 대기(DART 본문) — 확인되면 주문")
            self._update(job, "waiting", "1주당 배정 확인 대기(DART 본문)")
            return False
        await self._yield_to_ulc(job)
        if job.attempts > 0 and await self._already_ordered(job):
            return True
        broker = self.hub.broker
        mode = job.mode
        job.attempts += 1
        self._update(job, "ordering")
        if mode == "market" or mode == "after_resume":
            result = await asyncio.to_thread(broker.buy, job.code, job.budget)
            price = basis_price if basis_price > 0 else result.price
            why = basis or ("첫 체결가" if mode == "after_resume" else "공시 전 현재가")
        else:
            quote = quote or await self._quote(job)
            upper = (quote or {}).get("upper") or 0
            if upper <= 0:
                self._update(job, "waiting", "상한가 조회 실패 — 재시도")
                return False
            price = basis_price if basis_price > 0 else upper
            why = basis or "상한가"
            qty = int(job.budget // price)
            if qty <= 0:
                raise _Skip(f"{why} {price:,.0f}원 — 금액 {job.budget:,}원으로 1주도 못 삼")
            result = await asyncio.to_thread(broker.buy_limit, job.code, qty, upper)
        if not result.ok:
            first = job.attempts == 1
            self._update(job, "waiting", f"주문 거부·실패({job.attempts}회): {result.message}")
            if first or job.attempts % 20 == 0:
                self._log(job, f"주문 실패 — {self.RETRY_SEC:g}초마다 재시도: {result.message}")
            return False
        self.hub.tag_order(result, "bonus", "무상증자 매수",
                           {"acptno": job.acptno, "disclosed_at": job.disclosed_at, "mode": mode,
                            "basis": why, "basis_price": price, "budget": job.budget})
        self._update(job, "sent", result.message, order_no=result.order_no or "",
                     qty=result.filled_qty, price=price, basis=why)
        self._log(job, f"매수 주문 전송 — {result.message} (수량 기준 {why} {price:,.0f}원)",
                  order_no=result.order_no, qty=result.filled_qty, price=price,
                  quote_raw=(quote or {}).get("raw"))  # ka10001 응답 원문(예상가 근거)
        self.hub.broker.invalidate()
        await self.hub.manual_trades.broadcast_async(job.code)
        await self.hub.refresh_orders()
        return True


class _ResumeWatch:
    """매매 재개(첫 체결) 감지. 시세 구독은 수동 매매 틱 루프가 하고 있으니 마지막 틱만 본다.

    - 체결이 ``SILENCE_SEC`` 이상 없다가(매매정지) 새 체결이 오면 재개.
    - 목표 시각 1분 전부터 새 체결이 오면 재개(정지가 감지 전에 시작됐거나 mock처럼 정지가 없을 때).
    """

    def __init__(self, mgr: BonusTradeManager, job: BuyJob, target: datetime) -> None:
        self.mgr, self.job, self.target = mgr, job, target
        tick = mgr.hub.data.last_tick(job.code)
        self.last = tick.time if tick else None
        self.changed_at = _time.monotonic()
        self.armed_at: Optional[int] = None   # 목표 1분 전 시점의 틱 시각

    def resumed(self) -> bool:
        if not self.mgr.hub.clock.is_open():
            return False
        tick = self.mgr.hub.data.last_tick(self.job.code)
        cur = tick.time if tick else None
        now = self.mgr._now()
        if now >= self.target - timedelta(minutes=1) and self.armed_at is None:
            self.armed_at = cur if cur is not None else -1
            return False
        if cur == self.last:
            return False
        silent = _time.monotonic() - self.changed_at
        self.last, self.changed_at = cur, _time.monotonic()
        if silent >= self.mgr.SILENCE_SEC:
            return True
        return self.armed_at is not None and cur != self.armed_at
