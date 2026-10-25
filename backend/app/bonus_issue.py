"""무상증자 공시 감시 → 자동 매수.

감시(``BonusWatcher``, 전 계좌 공유)
  KIND 공시검색('무상증자')을 ``POLL_SEC``마다 조회해 새 접수번호를 찾는다. 키움 REST에는
  공시 API가 없다. 대상 = 제목에 '무상증자'·'결정'이 있고 정정·종속회사 공시가 아닌 유가·코스닥
  최초 공시. 유무상증자(``paid_too``)는 따로 표시한다(지난 조사에서 권리락 전날까지 중앙 −39%).
  처음 보는 공시라도 공시 시각이 ``MAX_AGE_MIN``분보다 오래됐으면(서버가 꺼져 있던 동안 등)
  목록에만 남기고 사지 않는다.

매수(``BonusTradeManager``, 계좌별)
  계좌별 스위치(기본 꺼짐)가 켜져 있으면 공시 종목을 **수동 매매 목록**(origin "bonus")에
  손절·익절(기본 평단 −10% / +25%, 전량 시장가)을 정해 올리고, 매수 작업(``BuyJob``)을 돌린다.
  손절·익절·보유·평단은 수동 매매와 같은 규칙(``manual_trade.ExitRule``, 계좌 잔고 기준)이다.

  장중 공시는 보통 매매정지 30분 + 재개 단일가 10분 = 공시 후 약 40분(``resume_min``)에 첫
  체결이 난다. 장전 공시는 그날 09:00, 장후·휴일·재개가 15:20을 넘는 공시는 다음 평일 09:00
  단일가가 목표(``target_at``)다. 휴장일은 모른다 — 그날 못 사면 기한(목표일 15:30)에 끝난다.

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
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

from .manual_trade import ExitRule
from .market_clock import KST, REGULAR_CLOSE, REGULAR_OPEN, now_kst

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


def fetch_kind(day: date) -> List[dict]:
    """(블로킹) KIND 공시검색 '무상증자' — 그날 공시 목록(최신순). 실패는 예외."""
    import requests

    r = requests.post("https://kind.krx.co.kr/disclosure/details.do", timeout=15,
                      headers={"User-Agent": "Mozilla/5.0"}, data={
        **_KIND_EMPTY, "method": "searchDetailsSub", "currentPageSize": 100, "pageIndex": 1,
        "orderMode": 1, "orderStat": "D", "forward": "details_sub", "reportNm": "무상증자",
        "reportNmTemp": "무상증자", "fromDate": day.isoformat(), "toDate": day.isoformat(),
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


class BonusWatcher:
    """전 계좌 공유 공시 감시. 새 무상증자 결정 공시를 각 계좌 ``bonus_trades``에 넘긴다."""

    POLL_SEC = 10.0
    ACTIVE_FROM, ACTIVE_TO = time(7, 0), time(20, 0)
    # 장 시작 직후는 [상한가 매매]의 시간이다. 이 사이 공시는 최대 2분 늦게 잡힌다(MAX_AGE_MIN 안).
    QUIET = (time(8, 59, 30), time(9, 1, 30))
    MAX_AGE_MIN = 10
    KEEP_EVENTS = 100

    def __init__(self, manager: "AccountManager", fetch: Callable[[date], List[dict]] = fetch_kind,
                 path: Optional[Path] = None) -> None:
        self.manager = manager
        self.fetch = fetch
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
                       "last_poll": None, "ok": None, "error": ""}
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

    def _persist(self) -> None:
        # 이레 넘은 접수번호는 잊는다(KIND는 그날 목록만 조회한다).
        cutoff = (now_kst().date() - timedelta(days=7)).isoformat()
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

    def should_poll(self, now: datetime) -> bool:
        t = now.time()
        return (now.weekday() < 5 and self.ACTIVE_FROM <= t < self.ACTIVE_TO
                and not (self.QUIET[0] <= t < self.QUIET[1]))

    # -- 감시 ----------------------------------------------------------------
    def start(self) -> None:
        if self.status["enabled"] and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

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
            rows = await asyncio.to_thread(self.fetch, now.date())
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
        found = [d for d in (classify(r) for r in rows) if d is not None and d.acptno not in self.seen]
        dispatched = []
        for d in sorted(found, key=lambda x: x.disclosed_at):
            self.seen[d.acptno] = d.at.date().isoformat()
            stale = now - d.at > timedelta(minutes=self.MAX_AGE_MIN)
            ev = {**asdict(d), "detected_at": now.isoformat(timespec="seconds"), "stale": stale}
            self.events.append(ev)
            tag = "유무상" if d.paid_too else "무상"
            note = (" — 대상 아님: " + d.why) if not d.eligible else (" — 오래된 공시라 매수 안 함" if stale else "")
            self.manager._log(f"📰 {tag}증자 공시: {d.corp}({d.code or '—'}) {d.at:%H:%M} {d.title}{note}")
            if d.eligible and not stale:
                dispatched.append(d)
        if found:
            self.events = self.events[-self.KEEP_EVENTS:]
            self._persist()
        for d in dispatched:
            for hub in self.manager.hubs.values():
                try:
                    await hub.bonus_trades.on_disclosure(d)
                except Exception:  # noqa: BLE001
                    logger.exception("[%s] 무상증자 매수 처리 오류(%s)", d.code, hub.account)
        if found:
            self._broadcast()
        return dispatched

    async def simulate(self, code: str, name: str, title: str = "", paid_too: bool = False,
                       minutes_ago: float = 0.0) -> Disclosure:
        """(데모) 가짜 공시를 감지한 것처럼 처리한다."""
        at = (now_kst() - timedelta(minutes=minutes_ago)).replace(second=0, microsecond=0)
        title = title or ("유무상증자결정" if paid_too else "무상증자결정")
        row = {"time": at.strftime("%Y-%m-%d %H:%M"), "corp": name or code, "title": title,
               "acptno": f"SIM{_time.time_ns()}", "code5": code[:5] if code.endswith("0") else code,
               "mkt": "ko"}
        d = classify(row)
        if d is None:
            raise ValueError("제목에 '무상증자'·'결정'이 있어야 합니다.")
        d.code = code
        orig, self.fetch = self.fetch, (lambda _day: [row])
        try:
            await self.poll_once()
        finally:
            self.fetch = orig
        return d


# ---------------------------------------------------------------- 계좌별 매수
@dataclass
class BonusSettings:
    enabled: bool = False            # 계좌별 자동 매수 스위치(기본 꺼짐)
    budget: int = 1_000_000          # 한 종목 매수 금액 상한
    order_mode: str = "expected"
    take_profit_pct: float = 25.0    # 평단 +% 이상 전량 매도
    stop_pct: float = 10.0           # 평단 −% 이하 전량 매도
    skip_above_pct: float = 0.0      # >0: 체결 기준가가 기준가 대비 이 % 이상이면 안 산다
    include_paid: bool = False       # 유무상증자도 산다
    lead_sec: int = 20               # expected: 단일가 마감 몇 초 전에 낼까
    resume_min: int = 40             # 장중 공시 → 첫 체결까지(정지 30분 + 재개 단일가 10분)

    @classmethod
    def parse(cls, data: dict, base: Optional["BonusSettings"] = None) -> "BonusSettings":
        cur = asdict(base or cls())
        for f in fields(cls):
            if f.name in (data or {}):
                cur[f.name] = data[f.name]
        try:
            s = cls(enabled=bool(cur["enabled"]), budget=int(float(cur["budget"])),
                    order_mode=str(cur["order_mode"]), take_profit_pct=float(cur["take_profit_pct"]),
                    stop_pct=float(cur["stop_pct"]), skip_above_pct=float(cur["skip_above_pct"] or 0),
                    include_paid=bool(cur["include_paid"]), lead_sec=int(float(cur["lead_sec"])),
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
        if not 0 <= s.skip_above_pct <= 30:
            raise ValueError("건너뛰기 기준은 0(끔)~30% 여야 합니다.")
        if not 3 <= s.lead_sec <= 120:
            raise ValueError("마감 전 주문 시각은 3~120초여야 합니다.")
        if not 1 <= s.resume_min <= 120:
            raise ValueError("재개까지 시간은 1~120분이어야 합니다.")
        return s


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

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.settings = BonusSettings()
        self.jobs: Dict[str, BuyJob] = {}
        self._path: Path = hub._state_path.with_suffix(".bonus.json")
        self._now: Callable[[], datetime] = now_kst

    # -- 조회·영속화 ---------------------------------------------------------
    def payload(self) -> dict:
        jobs = sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)
        return {"settings": asdict(self.settings), "jobs": [j.to_dict() for j in jobs]}

    def broadcast(self) -> None:
        self.hub.broadcast({"type": "bonus", **self.payload()})

    def persist(self) -> None:
        data = {"settings": asdict(self.settings), "jobs": [j.to_dict() for j in self.jobs.values()]}
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
        try:
            self.settings = BonusSettings.parse(raw.get("settings") or {})
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
        skip = f", 기준가 +{s.skip_above_pct:g}% 이상 건너뜀" if s.skip_above_pct > 0 else ""
        return (f"{s.budget:,}원 {mode}, 익절 +{s.take_profit_pct:g}%, 손절 −{s.stop_pct:g}%{skip}"
                + (", 유무상 포함" if s.include_paid else ""))

    # -- 공시 → 매수 작업 -----------------------------------------------------
    async def on_disclosure(self, d: Disclosure) -> Optional[BuyJob]:
        s = self.settings
        hub = self.hub
        label = f"[{d.code}] 무상증자 공시({d.corp})"
        if not s.enabled:
            hub._log(f"{label}: 이 계좌는 자동 매수가 꺼져 있어 건너뜀")
            return None
        if d.paid_too and not s.include_paid:
            hub._log(f"{label}: 유무상증자라 건너뜀(설정에서 켤 수 있음)")
            return None
        prev = self.jobs.get(d.code)
        if prev is not None and prev.status in ACTIVE_JOB:
            hub._log(f"{label}: 이미 매수 대기 중이라 건너뜀")
            return None
        if d.code in hub.manual_trades.items:
            hub._log(f"{label}: 이미 수동 매매 목록에 있어 건너뜀")
            return None
        exits = (ExitRule(enabled=True, mode="pct", pct=s.stop_pct),
                 ExitRule(enabled=True, mode="pct", pct=s.take_profit_pct))
        name = d.corp  # KIND 회사명 — 키움 종목명 조회(REST)를 아낀다
        try:
            # 공시 제목은 매수 작업(job)에 둔다 — 수동 매매 notice 는 '확인할 알림'(칩 ⚠)이다.
            hub.manual_trades.add(d.code, name or d.corp, "bonus", exits=exits)
        except ValueError as e:
            hub._log(f"{label}: 건너뜀 — {e}")
            return None
        now = self._now()
        job = BuyJob(code=d.code, name=name or d.corp, acptno=d.acptno, disclosed_at=d.disclosed_at,
                     title=d.title, mode=s.order_mode, budget=s.budget,
                     target_at=self._target(d.at).isoformat(timespec="seconds"),
                     created_at=now.isoformat(timespec="seconds"),
                     updated_at=now.isoformat(timespec="seconds"))
        self.jobs[d.code] = job
        self.persist()
        self._log(job, f"매수 대기 시작 — {self.describe()}, 첫 체결 예상 {job.target_at[11:16]}"
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
        self._log(job, "매수 대기 취소")
        return self.payload()

    def _start(self, job: BuyJob) -> None:
        if job.task is None or job.task.done():
            job.task = asyncio.create_task(self._run(job))

    async def close(self) -> None:
        tasks = [j.task for j in self.jobs.values() if j.task is not None]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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
            if mode in ("market", "upper_limit"):
                await self._run_now(job)
            elif mode == "expected":
                await self._run_expected(job)
            else:
                await self._run_after_resume(job)
        except asyncio.CancelledError:
            raise
        except _Skip as e:
            self._update(job, "skipped", str(e))
            self._log(job, f"매수 안 함 — {e}")
        except TimeoutError as e:
            self._update(job, "failed", str(e))
            self._log(job, f"⚠️ 매수 못 함 — {e}")
        except Exception as e:  # noqa: BLE001
            logger.exception("[%s] 무상증자 매수 작업 오류", job.code)
            self._update(job, "failed", f"오류: {type(e).__name__}")
            self._log(job, f"⚠️ 매수 작업 오류: {type(e).__name__} — 로그 확인 필요")

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

    async def _yield_to_ulc(self, job: BuyJob) -> None:
        """키움 호출(조회·주문) 전에: 상한가 1차 주문이 남아 있으면 먼저 보내게 기다린다."""
        if not self._ulc_pending():
            return
        self._log(job, f"상한가 매매 1차 주문을 먼저 보냅니다 — 키움 호출 대기(최대 {self.ULC_YIELD_MAX_SEC:g}초)")
        started = _time.monotonic()
        while self._ulc_pending() and _time.monotonic() - started < self.ULC_YIELD_MAX_SEC:
            self._check_deadline(job)
            await asyncio.sleep(self.STEP_SEC)

    def _skip_check(self, job: BuyJob, price: float, base: float, what: str) -> None:
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
        await self._yield_to_ulc(job)
        if job.attempts > 0 and await self._already_ordered(job):
            return True
        broker = self.hub.broker
        mode = job.mode
        job.attempts += 1
        self._update(job, "ordering")
        if mode == "market" or mode == "after_resume":
            result = await asyncio.to_thread(broker.buy, job.code, job.budget)
            price, why = result.price, ("첫 체결가" if mode == "after_resume" else "공시 전 현재가")
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
                  order_no=result.order_no, qty=result.filled_qty, price=price)
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
