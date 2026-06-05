"""키움증권 REST/WebSocket 클라이언트 — self-contained.

`kiwoom_rest_api_full_v3.md` 공식 명세를 기준으로 Bach 내부에 직접 구현했다.
외부 `kiwoom` 프로젝트를 런타임에 import 하지 않는다.

⚠️ 명세서만 보면 빠지는 함정들(키움 파이썬 코드에서 확인):

1. **주문 필드 시프트 (kt10000/kt10001)** — 명세 표에서 "0:보통,3:시장가,6:최유리…"
   설명이 `ord_uv` 행에 붙어 있으나 실제로는 `trde_tp`(매매구분) 값이다.
   시장가 주문 = ``trde_tp="3"``, ``ord_uv=""``. `dmst_stex_tp="KRX"` 필수.
2. **0B 실시간 거래량 FID** — 거래량은 FID 15(체결량)/13(누적거래량)이다.
   FID 11은 *전일대비*(가격 변화)이므로 거래량으로 쓰면 안 된다.
   가격=10, 시가=16, 고가=17, 저가=18.
3. **부호/제로패딩 가격** — 모든 가격 문자열은 ``+/-`` 부호와 0-padding을 포함한다.
   반드시 ``abs()`` + strip 으로 파싱한다(부호는 등락 방향일 뿐).
4. **계좌 종목코드 접두 'A'** — kt00018 응답의 ``stk_cd``는 ``A005930`` 형태 →
   ``lstrip("A")`` 필요.
5. **호스트 분기** — mock(모의투자)/live(실전) 호스트를 KIWOOM_MOCK 으로 분기한다.
6. **WebSocket PING** — 서버가 보내는 ``trnm:"PING"`` 메시지를 그대로 echo 해야
   연결이 유지된다. ``LOGIN`` 응답 ``return_code==0`` 확인 후 ``REG`` 등록.
7. **분봉 응답** — ``ka10080``은 최신봉부터(newest-first) 반환하며 cont-yn/next-key로
   페이징한다. 체결일시는 ``cntr_dt``/``cntr_tm``(YYYYMMDDHHMMSS), 종가는 ``cur_prc``
   (``close_pric`` 아님), 거래량은 ``trde_qty``.
"""
from __future__ import annotations

import calendar
import logging
import threading
import time as _time
from datetime import datetime
from typing import Dict, List, Optional

import requests

logger = logging.getLogger("bach.kiwoom")

# ---------------------------------------------------------------------------
# REST 호출 게이트 (함정: 동시/연속 호출 시 키움이 HTTP 429로 rate-limit)
# 종목 여러 개를 한꺼번에 추가하면 ka10080가 동시에 날아가 일부가 429 → 빈 봉.
# 전역 슬롯 예약으로 호출을 MIN_REST_INTERVAL 간격으로 직렬화하고, 429가 보이면
# 전역 쿨다운을 늘려 모든 호출자가 함께 백오프하도록 한다.
# ---------------------------------------------------------------------------
_rest_lock = threading.Lock()
_next_allowed = 0.0           # 다음 호출이 허용되는 monotonic 시각
MIN_REST_INTERVAL = 0.35      # 초당 ~3건


def _reserve_slot() -> None:
    """전역 직렬화: 자기 슬롯 시각을 예약하고(락은 짧게) 그 시각까지 대기."""
    global _next_allowed
    with _rest_lock:
        now = _time.monotonic()
        start = max(now, _next_allowed)
        _next_allowed = start + MIN_REST_INTERVAL
        wait = start - now
    if wait > 0:
        _time.sleep(wait)


def _penalize(seconds: float) -> None:
    """429 등으로 전역 쿨다운을 늘린다(이후 모든 호출자가 함께 백오프)."""
    global _next_allowed
    with _rest_lock:
        _next_allowed = max(_next_allowed, _time.monotonic() + seconds)


def _post(url: str, headers: dict, body: dict, timeout: float,
          retries: int = 5) -> Optional[requests.Response]:
    """게이트를 거친 POST. 429면 백오프 후 재시도. 최종 응답(또는 None) 반환."""
    resp: Optional[requests.Response] = None
    for attempt in range(retries):
        _reserve_slot()
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        except requests.RequestException as e:  # 네트워크 오류도 잠깐 백오프 후 재시도
            logger.warning("REST 연결 오류(%s/%s): %s", attempt + 1, retries, e)
            _penalize(0.4 * (attempt + 1))
            continue
        if resp.status_code == 429:
            _penalize(0.5 * (attempt + 1))
            continue
        return resp
    return resp

# ---------------------------------------------------------------------------
# 호스트 (mock=모의투자, live=실전)
# ---------------------------------------------------------------------------
_LIVE_REST = "https://api.kiwoom.com"
_MOCK_REST = "https://mockapi.kiwoom.com"
_LIVE_WS = "wss://api.kiwoom.com:10000/api/dostk/websocket"
_MOCK_WS = "wss://mockapi.kiwoom.com:10000/api/dostk/websocket"


def rest_host(mock: bool) -> str:
    return _MOCK_REST if mock else _LIVE_REST


def ws_url(mock: bool) -> str:
    return _MOCK_WS if mock else _LIVE_WS


# ---------------------------------------------------------------------------
# 파싱 유틸
# ---------------------------------------------------------------------------
def parse_price(val) -> float:
    """키움 가격 문자열 파싱: 부호(+/-)·쉼표·제로패딩 제거 후 절댓값."""
    s = str(val).strip().replace(",", "").replace("+", "")
    if not s or s in ("nan", "None"):
        return 0.0
    try:
        return abs(float(s))
    except ValueError:
        return 0.0


def parse_int(val) -> int:
    return int(parse_price(val))


def _kst_epoch(yyyymmddhhmmss: str) -> int:
    """'YYYYMMDDHHMMSS'(KST) → epoch seconds.

    lightweight-charts는 UTC로 렌더링하므로, KST 벽시계 시각이 그대로 보이도록
    naive KST 시각을 UTC인 것처럼 timegm 으로 환산한다(시간축 일관성 목적).
    """
    s = str(yyyymmddhhmmss).strip()
    s = (s + "000000")[:14] if len(s) < 14 else s[:14]
    dt = datetime.strptime(s, "%Y%m%d%H%M%S")
    return calendar.timegm(dt.timetuple())


# ---------------------------------------------------------------------------
# OAuth 토큰 (au10001)
# ---------------------------------------------------------------------------
def get_access_token(appkey: str, secretkey: str, mock: bool = False) -> str:
    url = f"{rest_host(mock)}/oauth2/token"
    headers = {"Content-Type": "application/json;charset=UTF-8"}
    body = {
        "grant_type": "client_credentials",
        "appkey": appkey,
        "secretkey": secretkey,
    }
    resp = requests.post(url, headers=headers, json=body, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("return_code") != 0 or not data.get("token"):
        raise RuntimeError(f"토큰 발급 실패: {data.get('return_msg')}")
    logger.info("✅ 키움 토큰 발급 성공 (만료 %s)", data.get("expires_dt"))
    return data["token"]


# ---------------------------------------------------------------------------
# 종목명 (ka10007 시세표성정보)
# ---------------------------------------------------------------------------
def fetch_quote(token: str, code: str, mock: bool = False) -> Optional[dict]:
    """ka10007로 종목명+현재가/시고저를 한 번에 조회. 실패 시 None.

    반환: {name, price, open, high, low}. 가격은 부호/패딩 처리된 절댓값.
    """
    url = f"{rest_host(mock)}/api/dostk/mrkcond"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka10007",
    }
    resp = _post(url, headers, {"stk_cd": code}, timeout=10, retries=3)
    if resp is None or resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if data.get("return_code") != 0:
        return None
    return {
        "name": (data.get("stk_nm") or "").strip() or None,
        "price": parse_price(data.get("cur_prc")),
        "open": parse_price(data.get("open_pric")),
        "high": parse_price(data.get("high_pric")),
        "low": parse_price(data.get("low_pric")),
    }


def fetch_stock_name(token: str, code: str, mock: bool = False) -> Optional[str]:
    """종목명만 필요할 때의 얇은 래퍼."""
    q = fetch_quote(token, code, mock=mock)
    return q.get("name") if q else None


# ---------------------------------------------------------------------------
# 분봉 차트 (ka10080)
# ---------------------------------------------------------------------------
def fetch_min_bars(
    token: str,
    code: str,
    tic_scope: int,
    mock: bool = False,
    today: Optional[str] = None,
    lookback_extra: int = 60,
    max_pages: int = 50,
) -> List[dict]:
    """분봉을 시간 오름차순(normalized)으로 반환.

    당일 봉 전체 + 당일 첫 봉 이전 ``lookback_extra``개(SMA 계산용)를 포함하도록
    충분히 페이징한다. 반환 dict: {time(epoch), open, high, low, close, volume, _date}.
    """
    import time as _time

    today = today or datetime.now().strftime("%Y%m%d")
    url = f"{rest_host(mock)}/api/dostk/chart"
    base_hdr = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka10080",
    }
    body = {"stk_cd": code, "tic_scope": str(tic_scope), "upd_stkpc_tp": "1"}

    rows: List[dict] = []
    cont_yn, next_key = "N", ""
    older_seen = 0  # today 이전 날짜 봉 수집 개수
    for _ in range(max_pages):
        hdr = dict(base_hdr)
        if cont_yn == "Y" and next_key:
            hdr["cont-yn"] = "Y"
            hdr["next-key"] = next_key
        resp = _post(url, hdr, body, timeout=15)
        if resp is None or resp.status_code != 200:
            sc = resp.status_code if resp is not None else "—"
            logger.warning("[%s] 분봉 조회 실패(HTTP %s)", code, sc)
            break
        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] 분봉 파싱 오류: %s", code, e)
            break
        if data.get("return_code") != 0:
            logger.warning("[%s] 분봉 API 오류: %s", code, data.get("return_msg"))
            break
        page = data.get("stk_min_pole_chart_qry") or []
        if isinstance(page, dict):
            page = [page]
        for raw in page:
            dt_raw = str(raw.get("cntr_dt") or raw.get("cntr_tm") or "").strip()
            d8 = dt_raw[:8]
            o = parse_price(raw.get("open_pric"))
            rows.append({
                "time": _kst_epoch(dt_raw),
                "open": o,
                "high": parse_price(raw.get("high_pric")) or o,
                "low": parse_price(raw.get("low_pric")) or o,
                "close": parse_price(raw.get("cur_prc")) or o,
                "volume": parse_price(raw.get("trde_qty")),
                "_date": d8,
            })
            if d8 and d8 < today:
                older_seen += 1
        cont_yn = resp.headers.get("cont-yn", "N")
        next_key = resp.headers.get("next-key", "")
        if older_seen >= lookback_extra or cont_yn != "Y" or not next_key:
            break
        _time.sleep(0.15)

    rows.sort(key=lambda r: r["time"])
    # 당일 첫 봉 인덱스 → 그 앞 lookback_extra개만 남기고 절삭
    first_today = next((i for i, r in enumerate(rows) if r["_date"] >= today), None)
    if first_today is None:
        return rows[-lookback_extra:] if lookback_extra else rows
    start = max(0, first_today - lookback_extra)
    return rows[start:]


# ---------------------------------------------------------------------------
# 일봉 차트 (ka10081) — 전일 종가(X) 산정용
# ---------------------------------------------------------------------------
def fetch_prev_close(token: str, code: str, mock: bool = False,
                     today: Optional[str] = None) -> Optional[float]:
    """직전 거래일 종가를 반환(없으면 None)."""
    today = today or datetime.now().strftime("%Y%m%d")
    url = f"{rest_host(mock)}/api/dostk/chart"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka10081",
    }
    body = {"stk_cd": code, "base_dt": today, "upd_stkpc_tp": "1"}
    resp = _post(url, headers, body, timeout=15)
    if resp is None or resp.status_code != 200:
        sc = resp.status_code if resp is not None else "—"
        logger.warning("[%s] 일봉 조회 실패(HTTP %s)", code, sc)
        return None
    try:
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] 일봉 파싱 오류: %s", code, e)
        return None
    if data.get("return_code") != 0:
        logger.warning("[%s] 일봉 API 오류: %s", code, data.get("return_msg"))
        return None
    rows = data.get("stk_dt_pole_chart_qry") or []
    if isinstance(rows, dict):
        rows = [rows]
    # 최신순. today(당일, 미확정)을 건너뛰고 직전 거래일 종가를 선택.
    for r in rows:
        d = str(r.get("dt") or "").strip()[:8]
        if d and d < today:
            return parse_price(r.get("cur_prc"))
    return None


# ---------------------------------------------------------------------------
# 주문 (kt10000 매수 / kt10001 매도)
# ---------------------------------------------------------------------------
def place_order(
    token: str,
    code: str,
    quantity: int,
    side: str,            # "buy" | "sell"
    mock: bool = False,
    order_type: str = "3",  # 매매구분(trde_tp): 3=시장가, 6=최유리지정가, 0=보통
    price: str = "",        # 주문단가(ord_uv): 시장가면 ""
) -> Optional[str]:
    """주문 전송 후 주문번호(ord_no) 반환, 실패 시 None.

    ⚠️ 시장가는 trde_tp="3"·ord_uv="" (명세표의 ord_uv 설명은 실제 trde_tp 값).
    """
    api_id = "kt10000" if side == "buy" else "kt10001"
    url = f"{rest_host(mock)}/api/dostk/ordr"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": api_id,
    }
    body = {
        "dmst_stex_tp": "KRX",
        "stk_cd": code,
        "ord_qty": str(quantity),
        "ord_uv": price,
        "trde_tp": order_type,
        "cond_uv": "",
    }
    resp = _post(url, headers, body, timeout=10, retries=3)
    if resp is None or resp.status_code != 200:
        sc = resp.status_code if resp is not None else "—"
        logger.error("[%s] %s 주문 실패(HTTP %s)", code, side, sc)
        return None
    try:
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.error("[%s] %s 주문 파싱 오류: %s", code, side, e)
        return None
    if data.get("return_code") != 0:
        logger.error("[%s] %s 주문 실패: %s", code, side, data.get("return_msg"))
        return None
    order_no = data.get("ord_no")
    logger.info("✅ [%s] %s 주문 성공: 주문번호 %s, 수량 %s", code, side, order_no, quantity)
    return order_no


# ---------------------------------------------------------------------------
# 계좌 잔고 (kt00018) — 보유 포지션
# ---------------------------------------------------------------------------
def fetch_account_summary(token: str, mock: bool = False) -> Optional[dict]:
    """계좌 요약: 예수금/주문가능금액(kt00001) + 평가금액/손익/총자산(kt00018).

    반환: {deposit, orderable, stock_eval, eval_pnl, purchase, total_asset} 또는 None.
    """
    host = rest_host(mock)
    base = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
    }
    out: dict = {}

    # 예수금/주문가능금액 (kt00001)
    r = _post(f"{host}/api/dostk/acnt", {**base, "api-id": "kt00001"},
              {"qry_tp": "2"}, timeout=10)
    if r is not None and r.status_code == 200:
        try:
            d = r.json()
            if d.get("return_code") == 0:
                out["deposit"] = parse_price(d.get("entr"))         # 예수금
                out["orderable"] = parse_price(d.get("ord_alow_amt"))  # 주문가능금액
        except Exception:  # noqa: BLE001
            pass

    # 평가금액/손익/총자산 (kt00018)
    r = _post(f"{host}/api/dostk/acnt", {**base, "api-id": "kt00018"},
              {"qry_tp": "1", "dmst_stex_tp": "KRX"}, timeout=10)
    if r is not None and r.status_code == 200:
        try:
            d = r.json()
            if d.get("return_code") == 0:
                out["stock_eval"] = parse_price(d.get("tot_evlt_amt"))   # 주식평가금액
                out["eval_pnl"] = _signed(d.get("tot_evlt_pl"))         # 총평가손익(부호)
                out["purchase"] = parse_price(d.get("tot_pur_amt"))     # 총매입금액
                out["total_asset"] = parse_price(d.get("prsm_dpst_aset_amt"))  # 추정예탁자산
        except Exception:  # noqa: BLE001
            pass

    return out or None


def fetch_unfilled(token: str, mock: bool = False) -> List[dict]:
    """계좌 미체결 주문(ka10075)을 한 번에 조회. 주문 dict 리스트 반환."""
    url = f"{rest_host(mock)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka10075",
    }
    body = {"all_stk_tp": "0", "trde_tp": "0", "stk_cd": "", "stex_tp": "0"}
    resp = _post(url, headers, body, timeout=10)
    if resp is None or resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return []
    if data.get("return_code") != 0:
        return []
    out: List[dict] = []
    for o in data.get("oso", []) or []:
        code = str(o.get("stk_cd", "")).lstrip("A").strip()
        if not code:
            continue
        io = str(o.get("io_tp_nm", ""))
        out.append({
            "code": code,
            "side": "sell" if "매도" in io else "buy",
            "order_type": str(o.get("trde_tp", "")).strip(),  # 시장가/지정가 등
            "qty": parse_int(o.get("ord_qty")),
            "unfilled": parse_int(o.get("oso_qty")),
            "filled": parse_int(o.get("cntr_qty")),
            "price": parse_price(o.get("ord_pric")),
            "order_no": str(o.get("ord_no", "")).strip(),
            "status": str(o.get("ord_stt", "")).strip(),  # 접수/확인 등
        })
    return out


def _signed(val) -> float:
    """부호 보존 파싱(평가손익 등 음수 의미 있는 값)."""
    s = str(val).strip().replace(",", "")
    if not s or s in ("nan", "None"):
        return 0.0
    try:
        return float(s.replace("+", ""))
    except ValueError:
        return 0.0


def fetch_positions(token: str, mock: bool = False) -> Dict[str, dict]:
    """보유 종목 dict 반환.

    {code: {quantity, avg_price, current_price, sellable_qty, eval_pnl}}.
    동일 종목 복수 로트는 수량 합산·가중평균 단가로 집계한다.
    """
    url = f"{rest_host(mock)}/api/dostk/acnt"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "kt00018",
    }
    body = {"qry_tp": "1", "dmst_stex_tp": "KRX"}
    resp = _post(url, headers, body, timeout=10)
    if resp is None or resp.status_code != 200:
        sc = resp.status_code if resp is not None else "—"
        logger.error("계좌 잔고 조회 실패(HTTP %s)", sc)
        return {}
    try:
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.error("계좌 잔고 파싱 오류: %s", e)
        return {}
    if data.get("return_code") != 0:
        logger.error("계좌 잔고 조회 실패: %s", data.get("return_msg"))
        return {}

    out: Dict[str, dict] = {}
    for item in data.get("acnt_evlt_remn_indv_tot", []) or []:
        code = str(item.get("stk_cd", "")).lstrip("A")
        if not code:
            continue
        qty = parse_int(item.get("rmnd_qty"))
        buy = parse_price(item.get("pur_pric"))
        cur = parse_price(item.get("cur_prc"))
        sellable = parse_int(item.get("trde_able_qty"))
        pnl = parse_price(item.get("evltv_prft"))  # 부호 소실: 표시용 근사
        if code in out:
            agg = out[code]
            tot_qty = agg["quantity"] + qty
            if tot_qty > 0:
                agg["avg_price"] = (
                    agg["avg_price"] * agg["quantity"] + buy * qty
                ) / tot_qty
            agg["quantity"] = tot_qty
            agg["sellable_qty"] += sellable
            agg["current_price"] = cur or agg["current_price"]
        else:
            out[code] = {
                "quantity": qty,
                "avg_price": buy,
                "current_price": cur,
                "sellable_qty": sellable,
                "eval_pnl": pnl,
            }
    return out
