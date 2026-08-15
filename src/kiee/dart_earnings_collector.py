"""
dart_earnings_collector.py
DART 무료 API로 산업별 분기 영업이익 YoY를 수집해
earnings_momentum 팩터의 정밀도를 높인다.

사용 Secret: DART_API_KEY (opendart.fss.or.kr)
출력: input/dart_earnings_raw.json

approach (수정판):
  1. corpCode.xml ZIP 1회 다운로드 → stock_code→corp_code 전체 매핑 (캐시 24h)
  2. 산업별 basket에서 상위 3개사 corp_code 일괄 조회 (API 호출 0회 추가)
  3. 최근 분기(월 기준 자동 선택) 영업이익 YoY 조회 (회사당 2회: 당해+전년)
  4. YoY 중앙값 → 점수 → earnings_momentum metric

변경 이유:
  - 구 방식: company.json?stock_code=... → corp_code 변환 (이 파라미터 미지원, 전부 실패)
  - 신 방식: corpCode.xml ZIP 한 번 받아 전체 매핑 → 개별 API 호출 불필요
"""
from __future__ import annotations

import io
import json
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .util import age_hours, clamp, read_json, utc_now_iso, write_json

DART_BASE        = "https://opendart.fss.or.kr/api"
OUTPUT_RAW       = "input/dart_earnings_raw.json"
CORPCODE_CACHE   = "input_cache/dart_corpcode_map.json"   # stock_code → corp_code 캐시
QUALITY_CAP      = 72.0
SHRINKAGE        = 0.80
# ZIP 1회 + 핵심 25개 산업 × 2개사 × 폴백 1년 = 최대 101회
MAX_CALLS         = 110
CACHE_TTL_H       = 24
CORPCODE_TTL_H    = 168
MIN_BASKET_CNT    = 1
# v3.2: 폭넓은 산업 커버리지를 위해 신규 gap 산업은 대표 1개사부터 수집한다.
# 1개사 관측은 실제 공시이지만 산업 대표성이 낮으므로 quality 산식에서는
# TARGET_FIRMS_FOR_FULL_QUALITY=2를 유지해 자동 감산한다.
MAX_FIRMS         = 3
TARGET_FIRMS_FOR_FULL_QUALITY = 2
MAX_YEAR_FALLBACK = 0
MAX_INDUSTRIES    = 50
API_SLEEP         = 0.05
COLLECTOR_VERSION = "dart-earnings-v3.4-axis-gap-retry"

# 분기 공시 코드
_REPRT_CODE = {"1Q": "11013", "HY": "11012", "3Q": "11014", "FY": "11011"}


# ── HTTP 유틸 ─────────────────────────────────────────────────────────────────

def _get_json(endpoint: str, params: dict[str, Any], api_key: str, timeout: int = 30) -> Any:
    p = dict(params)
    p["crtfc_key"] = api_key
    p["type"] = "json"
    url = f"{DART_BASE}/{endpoint}?{urllib.parse.urlencode(p)}"
    req = urllib.request.Request(url, headers={"User-Agent": "kiee-dart-earnings/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_bytes(endpoint: str, params: dict[str, Any], api_key: str, timeout: int = 60) -> bytes:
    p = dict(params)
    p["crtfc_key"] = api_key
    url = f"{DART_BASE}/{endpoint}?{urllib.parse.urlencode(p)}"
    req = urllib.request.Request(url, headers={"User-Agent": "kiee-dart-earnings/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _rows(data: Any, key: str = "list") -> list[dict]:
    if not isinstance(data, dict):
        return []
    return [r for r in (data.get(key) or []) if isinstance(r, dict)]


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        n = float(str(value).replace(",", "").strip())
        return n if n == n and abs(n) < 1e18 else None
    except (TypeError, ValueError):
        return None


# ── corp_code 맵 (ZIP 1회 다운로드, 캐시) ─────────────────────────────────────

def _load_corpcode_map(root: Path, api_key: str, call_count: list[int]) -> dict[str, str]:
    """
    stock_code → corp_code 전체 매핑을 반환.
    캐시가 유효하면 API 호출 없이 반환.
    캐시 만료/없으면 corpCode.xml ZIP을 1회 다운로드하여 파싱 후 캐시.
    """
    cache_path = root / CORPCODE_CACHE
    cached = read_json(cache_path, {})

    if isinstance(cached, dict) and cached.get("map"):
        age = age_hours(cached.get("fetched_at"))
        if age is not None and age < CORPCODE_TTL_H:
            return cached["map"]

    # ZIP 다운로드 (1회 API 호출)
    try:
        call_count[0] += 1
        raw = _get_bytes("corpCode.xml", {}, api_key)
        zf = zipfile.ZipFile(io.BytesIO(raw))
        xml_bytes = zf.read("CORPCODE.xml")
        root_elem = ET.fromstring(xml_bytes.decode("utf-8"))
    except Exception as e:
        # 다운로드 실패 시 캐시된 맵 재사용 (만료됐어도)
        if isinstance(cached, dict) and cached.get("map"):
            return cached["map"]
        return {}

    corp_map: dict[str, str] = {}
    corp_names: dict[str, str] = {}
    for item in root_elem.iter("list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        corp_code  = (item.findtext("corp_code")  or "").strip()
        corp_name  = (item.findtext("corp_name")  or "").strip()
        if stock_code and corp_code:
            corp_map[stock_code] = corp_code
            if corp_name:
                corp_names[stock_code] = corp_name

    write_json(cache_path, {"fetched_at": utc_now_iso(), "map": corp_map, "names": corp_names})
    return corp_map


# ── 분기 영업이익 조회 ────────────────────────────────────────────────────────

def _fetch_financials(corp_code: str, year: int, reprt_code: str, api_key: str, cache: dict[tuple[str,int,str], tuple[float | None,float | None]] | None = None) -> tuple[float | None, float | None]:
    """Return (operating_profit, revenue) from one full-account request per fs_div.

    ``fnlttSinglAcntAll`` already returns both accounts, so reading revenue here
    adds no DART API call versus the previous operating-profit-only collector.
    """
    cache_key = (corp_code, year, reprt_code)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    for fs_div in ("CFS", "OFS"):
        try:
            data = _get_json("fnlttSinglAcntAll.json", {
                "corp_code": corp_code,
                "bsns_year": str(year),
                "reprt_code": reprt_code,
                "fs_div": fs_div,
            }, api_key)
            if data.get("status") not in ("000", None):
                continue
            op: float | None = None
            revenue: float | None = None
            for row in _rows(data):
                nm = str(row.get("account_nm") or "").strip()
                aid = str(row.get("account_id") or "").strip()
                val = _number(row.get("thstrm_amount") or row.get("thstrm_add_amount"))
                if val is None:
                    continue
                if nm in ("영업이익", "영업이익(손실)") or "ProfitLossFromOperating" in aid:
                    op = val
                if nm in ("매출액", "수익(매출액)", "영업수익") or aid.endswith("Revenue") or "RevenueFromContractsWithCustomers" in aid:
                    revenue = val
            if op is not None or revenue is not None:
                if cache is not None:
                    cache[cache_key] = (op, revenue)
                return op, revenue
        except Exception:
            pass
    if cache is not None:
        cache[cache_key] = (None, None)
    return None, None


# ── 분기 자동 선택 (현재 월 기준) ────────────────────────────────────────────

def _select_quarter(month: int) -> tuple[str, str]:
    """
    현재 월 → (조회할 분기 코드, 공시 reprt_code).
    공시 지연(약 45일)을 고려해 한 분기 전 데이터를 조회.
    """
    if month >= 11:   # 11월 이후 → 3Q(9월 말) 공시 확정
        return "3Q", _REPRT_CODE["3Q"]
    elif month >= 8:  # 8월 이후 → HY(6월 말)
        return "HY", _REPRT_CODE["HY"]
    elif month >= 5:  # 5월 이후 → 1Q(3월 말)
        return "1Q", _REPRT_CODE["1Q"]
    else:             # 1~4월 → 전년 FY
        return "FY", _REPRT_CODE["FY"]


# ── 산업 실적 모멘텀 계산 ─────────────────────────────────────────────────────

def _surprise_flag(yoy_list: list[float]) -> bool:
    if len(yoy_list) < 2:
        return False
    return yoy_list[-1] > 0 and yoy_list[-1] > sum(yoy_list[:-1]) / len(yoy_list[:-1]) * 1.5


def _fetch_op_with_fallback(
    corp_code: str,
    base_year: int,
    reprt_code: str,
    api_key: str,
    call_count: list[int],
    max_calls: int,
    financial_cache: dict[tuple[str,int,str], tuple[float | None,float | None]],
) -> tuple[float | None, float | None, float | None, float | None, int]:
    """Return current/previous OP and revenue using the same two account calls."""
    for offset in range(MAX_YEAR_FALLBACK + 1):
        ref_year = base_year - offset
        if call_count[0] + 2 > max_calls:
            return None, None, None, None, ref_year
        cur_key = (corp_code, ref_year, reprt_code)
        prev_key = (corp_code, ref_year - 1, reprt_code)
        if cur_key not in financial_cache:
            if call_count[0] + 1 > max_calls:
                return None, None, None, None, ref_year
            call_count[0] += 1
            cur_op, cur_rev = _fetch_financials(corp_code, ref_year, reprt_code, api_key, financial_cache)
            time.sleep(API_SLEEP)
        else:
            cur_op, cur_rev = financial_cache[cur_key]
        if prev_key not in financial_cache:
            if call_count[0] + 1 > max_calls:
                return None, None, None, None, ref_year
            call_count[0] += 1
            prev_op, prev_rev = _fetch_financials(corp_code, ref_year - 1, reprt_code, api_key, financial_cache)
            time.sleep(API_SLEEP)
        else:
            prev_op, prev_rev = financial_cache[prev_key]
        op_ok = cur_op is not None and prev_op is not None and prev_op != 0
        rev_ok = cur_rev is not None and prev_rev is not None and prev_rev != 0
        if op_ok or rev_ok:
            return cur_op, prev_op, cur_rev, prev_rev, ref_year
    return None, None, None, None, base_year


def collect_industry(
    industry: dict[str, Any],
    api_key: str,
    corp_map: dict[str, str],
    call_count: list[int],
    max_calls: int,
    current_year: int,
    qcode: str,
    reprt_code: str,
    financial_cache: dict[tuple[str,int,str], tuple[float | None,float | None]] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """(metric | None, detail_msg) 반환."""
    if financial_cache is None:
        financial_cache = {}
    basket = industry.get("krx_basket") or []
    if len(basket) < MIN_BASKET_CNT:
        return None, "basket<min"

    # stock_code → corp_code 변환 (API 호출 없음)
    firms: list[tuple[str, str]] = []
    map_misses: list[str] = []
    for sc in basket[:max(MAX_FIRMS * 2, 6)]:
        key6 = str(sc).zfill(6)
        cc = corp_map.get(key6) or corp_map.get(str(sc))
        if cc:
            firms.append((sc, cc))
        else:
            map_misses.append(key6)
        if len(firms) >= MAX_FIRMS:
            break

    if not firms:
        return None, f"corp_map miss: {map_misses[:3]}"

    yoy_list: list[float] = []
    revenue_yoy_list: list[float] = []
    margin_delta_list: list[float] = []
    used_year = current_year
    api_empty_count = 0

    for stock_code, corp_code in firms:
        cur_op, prev_op, cur_rev, prev_rev, actual_year = _fetch_op_with_fallback(
            corp_code, current_year, reprt_code, api_key, call_count, max_calls, financial_cache
        )
        observed = False
        if cur_op is not None and prev_op is not None and prev_op != 0:
            yoy = (cur_op / abs(prev_op) - 1.0) * 100.0
            yoy_list.append(yoy)
            observed = True
            if cur_rev not in (None, 0) and prev_rev not in (None, 0):
                cur_margin = cur_op / abs(cur_rev) * 100.0
                prev_margin = prev_op / abs(prev_rev) * 100.0
                margin_delta_list.append(cur_margin - prev_margin)
        if cur_rev is not None and prev_rev is not None and prev_rev != 0:
            revenue_yoy_list.append((cur_rev / abs(prev_rev) - 1.0) * 100.0)
            observed = True
        if observed:
            used_year = actual_year
        else:
            api_empty_count += 1

    if not yoy_list and not revenue_yoy_list:
        detail = f"API empty (map_ok={len(firms)}, misses={map_misses[:2]})"
        if map_misses:
            detail += f", corp_map miss: {map_misses[:2]}"
        return None, detail

    n_fetched = len(yoy_list)
    median_yoy = None
    pct_score = None
    score = None
    if yoy_list:
        yoy_list.sort()
        median_yoy = yoy_list[len(yoy_list) // 2]
        pct_score  = clamp(50.0 + median_yoy / 100.0 * 50.0, 0.0, 100.0)
        score      = clamp(50.0 + (pct_score - 50.0) * SHRINKAGE, 0.0, 100.0)
    # 폴백 연도 사용 시 quality 10% 감산. 대표 1개사만 있으면 산업 대표성 때문에
    # full-quality(2개사) 대비 자동 감산한다.
    freshness  = 1.0 if used_year >= current_year else max(0.7, 1.0 - 0.1 * (current_year - used_year))
    quality_basis_n = max(n_fetched, len(revenue_yoy_list))
    quality = clamp(QUALITY_CAP * (0.5 + 0.5 * min(quality_basis_n / TARGET_FIRMS_FOR_FULL_QUALITY, 1.0)) * freshness, 0.0, QUALITY_CAP)
    surprise   = _surprise_flag(yoy_list) if yoy_list else False
    fallback_note = f" (폴백 {used_year}년 기준)" if used_year < current_year else ""
    median_margin_delta = None
    margin_score = None
    margin_quality = 0.0
    if margin_delta_list:
        margin_delta_list.sort()
        median_margin_delta = margin_delta_list[len(margin_delta_list) // 2]
        # ±10%p margin change spans the useful 0~100 range, then shrink toward neutral.
        raw_margin_score = clamp(50.0 + median_margin_delta * 5.0, 0.0, 100.0)
        margin_score = clamp(50.0 + (raw_margin_score - 50.0) * 0.75, 0.0, 100.0)
        margin_quality = clamp(QUALITY_CAP * (0.45 + 0.55 * min(len(margin_delta_list) / TARGET_FIRMS_FOR_FULL_QUALITY, 1.0)) * freshness, 0.0, QUALITY_CAP)

    median_revenue_yoy = None
    revenue_score = None
    revenue_quality = 0.0
    if revenue_yoy_list:
        revenue_yoy_list.sort()
        median_revenue_yoy = revenue_yoy_list[len(revenue_yoy_list) // 2]
        raw_revenue_score = clamp(50.0 + median_revenue_yoy / 100.0 * 50.0, 0.0, 100.0)
        revenue_score = clamp(50.0 + (raw_revenue_score - 50.0) * 0.72, 0.0, 100.0)
        revenue_quality = clamp(QUALITY_CAP * (0.5 + 0.5 * min(len(revenue_yoy_list) / TARGET_FIRMS_FOR_FULL_QUALITY, 1.0)) * freshness, 0.0, QUALITY_CAP)

    metric = {
        "id":                  f"dart_earnings_{industry['key']}",
        "factor":              "earnings_momentum",
        "value":               round(median_yoy, 2) if median_yoy is not None else round(median_revenue_yoy, 2),
        "unit":                "영업이익 YoY % (중앙값)" if median_yoy is not None else "매출액 YoY % (중앙값)",
        "long_run_percentile": round(pct_score, 3) if pct_score is not None else round(revenue_score, 3),
        "score":               round(score, 4) if score is not None else None,
        "quality":             round(quality, 1) if score is not None else 0.0,
        "source":              f"DART 공시 {qcode} 실제 실적 ({max(n_fetched, len(revenue_yoy_list))}개사){fallback_note}",
        "series_id":           f"dart_op_{industry['key']}",
        "as_of":               f"{used_year}-{qcode}",
        "available":           True,
        "is_dart_earnings":    True,
        "median_yoy_pct":      round(median_yoy, 2) if median_yoy is not None else None,
        "n_firms":             n_fetched,
        "median_revenue_yoy_pct": round(median_revenue_yoy, 2) if median_revenue_yoy is not None else None,
        "revenue_score":       round(revenue_score, 4) if revenue_score is not None else None,
        "revenue_quality":     round(revenue_quality, 1),
        "revenue_n_firms":     len(revenue_yoy_list),
        "api_empty_firms":     api_empty_count,
        "surprise":            surprise,
        "reference_year":      used_year,
        "median_margin_delta_ppt": round(median_margin_delta, 3) if median_margin_delta is not None else None,
        "margin_score":        round(margin_score, 4) if margin_score is not None else None,
        "margin_quality":      round(margin_quality, 1),
        "margin_n_firms":      len(margin_delta_list),
        "note":                f"DART corpCode ZIP 매핑 → fnlttSinglAcntAll{fallback_note}. 동일 응답에서 매출액 YoY·영업이익 YoY·영업마진 변화를 함께 계산. 대표 1개사 관측은 quality를 자동 감산.",
    }
    detail = f"op={score if score is not None else 'NA'} revenue={revenue_score if revenue_score is not None else 'NA'} n={max(n_fetched,len(revenue_yoy_list))} year={used_year}"
    return metric, detail


# ── gap 우선순위 / 누적 캐시 ─────────────────────────────────────────────────

def _cycle_direct_axes(root: Path) -> dict[str, set[str]]:
    """KOSIS/공식 cycle feed에서 이미 직접 관측된 core 축을 보수적으로 추정."""
    data = read_json(root / "input" / "industry_cycle_latest.json", {}) or {}
    out: dict[str, set[str]] = {}
    for row in data.get("industries") or []:
        key = str(row.get("industry_key") or "")
        axes: set[str] = set()
        metrics = ((row.get("current") or {}).get("metrics") or [])
        factors = {str(m.get("factor") or "") for m in metrics if isinstance(m, dict) and m.get("available") is True}
        if factors & {"production_shipments", "sales_earnings", "employment", "earnings_momentum"}:
            axes.add("earnings_momentum")
        if factors & {"production_shipments", "sales_earnings", "utilization", "pmi_bsi", "employment", "demand_cycle"}:
            axes.add("demand_cycle")
        if factors & {"price_margin", "pricing_margin"}:
            axes.add("pricing_margin")
        if key:
            out[key] = axes
    return out

def _previous_rows_same_period(previous: dict[str, Any], qcode: str, year: int) -> dict[str, dict[str, Any]]:
    if not isinstance(previous, dict):
        return {}
    if str(previous.get("quarter") or "") != qcode or int(previous.get("reference_year") or -1) != year:
        return {}
    return {str(r.get("industry_key") or ""): r for r in (previous.get("industries") or []) if isinstance(r, dict) and r.get("industry_key")}

def _row_metric(row: dict[str, Any]) -> dict[str, Any]:
    metrics = ((row.get("current") or {}).get("metrics") or []) if isinstance(row, dict) else []
    return next((m for m in metrics if isinstance(m, dict)), {})

def _row_has_revenue_signal(row: dict[str, Any]) -> bool:
    return _row_metric(row).get("revenue_score") is not None

def _row_has_op_signal(row: dict[str, Any]) -> bool:
    m = _row_metric(row)
    return m.get("score") is not None and int(m.get("n_firms") or 0) > 0

def _row_has_margin_signal(row: dict[str, Any]) -> bool:
    m = _row_metric(row)
    return m.get("margin_score") is not None and int(m.get("margin_n_firms") or 0) > 0

def _row_satisfies_missing_axes(row: dict[str, Any], direct_axes: set[str]) -> bool:
    """True only when DART already fills every core axis still missing from official cycle data."""
    if "earnings_momentum" not in direct_axes and not _row_has_op_signal(row):
        return False
    if "demand_cycle" not in direct_axes and not _row_has_revenue_signal(row):
        return False
    if "pricing_margin" not in direct_axes and not _row_has_margin_signal(row):
        return False
    return True

# ── 메인 수집 ─────────────────────────────────────────────────────────────────

def collect(root: Path, force: bool = False) -> dict[str, Any]:
    output_path = root / OUTPUT_RAW
    api_key = os.getenv("DART_API_KEY", "").strip()

    def _pending(reason: str, calls: int = 0, diag: list[str] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": "1.0.0", "status": "pending",
            "generated_at_utc": utc_now_iso(), "industries": [],
            "collector": COLLECTOR_VERSION, "reason": reason,
            "external_calls": calls,
        }
        if diag:
            result["diagnostics"] = diag
        write_json(output_path, result)
        return result

    if not api_key:
        return _pending("DART_API_KEY 미설정. GitHub Secret에 DART_API_KEY를 추가하세요.")

    # 캐시 유효성 확인
    if not force:
        prev = read_json(output_path, {}) or {}
        if isinstance(prev, dict) and prev.get("status") in {"raw", "scored"}:
            age = age_hours(prev.get("generated_at_utc"))
            # v3.4는 revenue만 있다고 완료 처리하지 않는다. earnings/margin gap 재시도를 위해
            # 같은 버전이어도 24h 동안 최소 1회는 gap-first 루프를 허용한다.
            if (prev.get("collector") == COLLECTOR_VERSION and age is not None and age < 1.0
                    and int(prev.get("axis_complete_industry_count") or 0) >= 95):
                prev["cache_hit"] = True
                return prev

    now          = datetime.now(timezone.utc)
    current_year = now.year
    current_month = now.month
    qcode, reprt_code = _select_quarter(current_month)

    # FY는 전년도 기준
    if qcode == "FY":
        current_year -= 1

    try:
        industries_raw = read_json(root / "config" / "industries.json", {})
        industries = (industries_raw or {}).get("industries") or []
    except Exception:
        return _pending("config/industries.json 로드 실패")

    call_count = [0]
    financial_cache: dict[tuple[str,int,str], tuple[float | None,float | None]] = {}

    # ── Step 1: corpCode ZIP 1회 다운로드 → 전체 매핑 ──────────────────────────
    corp_map = _load_corpcode_map(root, api_key, call_count)
    if not corp_map:
        return _pending("corpCode.xml 다운로드 실패 — DART_API_KEY 확인 필요", call_count[0])

    # ── Step 2: gap 산업 우선 수집 + 동일 분기 누적 ───────────────────────────
    previous = read_json(output_path, {}) or {}
    previous_rows = _previous_rows_same_period(previous, qcode, current_year)
    direct_axes = _cycle_direct_axes(root)
    complete_keys = {key for key, row in previous_rows.items() if _row_satisfies_missing_axes(row, direct_axes.get(key, set()))}

    def priority(ind: dict[str, Any]) -> tuple[int, int]:
        key = str(ind.get("key") or "")
        axes = direct_axes.get(key, set())
        missing_core = sum(1 for axis in ("earnings_momentum", "demand_cycle", "pricing_margin") if axis not in axes)
        # 모든 필요한 축을 실제 DART 신호로 채우지 못한 산업을 우선 재시도한다.
        return (1 if key not in complete_keys else 0, missing_core)

    candidates = sorted(industries, key=priority, reverse=True)
    # 같은 분기 기존 row는 보존하되, revenue_score가 없는 구버전 row는 gap-first로 재수집해 교체한다.
    results_by_key: dict[str, dict[str, Any]] = dict(previous_rows)
    diagnostics: list[str] = [
        f"corp_map_size={len(corp_map)} quarter={qcode} year={current_year} calls_used={call_count[0]}",
        f"previous_same_period={len(previous_rows)} revenue_complete={len(complete_keys)} gap_first=true",
    ]
    attempted = 0

    for ind in candidates:
        key = str(ind.get("key") or "")
        if not key or key in complete_keys:
            continue
        if attempted >= MAX_INDUSTRIES:
            break
        attempted += 1
        if call_count[0] >= MAX_CALLS:
            diagnostics.append("call cap reached")
            break
        try:
            metric, detail = collect_industry(
                ind, api_key, corp_map, call_count, MAX_CALLS,
                current_year, qcode, reprt_code, financial_cache,
            )
            if metric:
                results_by_key[key] = {
                    "industry_key": key,
                    "current": {"metrics": [metric], "dart_earnings": True},
                }
                diagnostics.append(f"{key}: {detail}")
            else:
                diagnostics.append(f"{key}: no data — {detail}")
        except Exception as e:
            diagnostics.append(f"{key}: error {str(e)[:80]}")

    results = list(results_by_key.values())
    revenue_enabled_count = sum(1 for row in results if _row_has_revenue_signal(row))
    axis_complete_count = sum(1 for key, row in results_by_key.items() if _row_satisfies_missing_axes(row, direct_axes.get(key, set())))
    result: dict[str, Any] = {
        "schema_version":       "1.0.0",
        "status":               "raw" if results else "pending",
        "generated_at_utc":     utc_now_iso(),
        "collector":            COLLECTOR_VERSION,
        "quarter":              qcode,
        "reference_year":       current_year,
        "industries":           results,
        "scored_industry_count": len(results),
        "revenue_enabled_industry_count": revenue_enabled_count,
        "axis_complete_industry_count": axis_complete_count,
        "external_calls":       call_count[0],
        "diagnostics":          diagnostics + [f"financial_cache_entries={len(financial_cache)}"],
        "note": (
            "DART corpCode ZIP 1회 다운로드로 전체 corp_code 매핑 후 "
            "fnlttSinglAcntAll 동일 응답에서 매출액 YoY·영업이익 YoY·영업이익률 변화를 수집. "
            "KOSIS 직접관측 gap 산업을 우선하며 같은 분기 기존 결과는 누적 재사용한다. 동일 대표기업을 여러 산업이 공유할 때 DART 재무응답을 run 내부 캐시로 재사용하고 gap 산업은 최대 3개 대표기업까지 확인한다. 매출만 확보된 row를 완료로 오인하지 않고 영업이익·마진까지 필요한 축을 재시도한다. earnings_momentum·demand_cycle·pricing_margin 직접관측 보강용."
        ),
    }
    if not results:
        result["reason"] = "영업이익 데이터 없음 — 공시 시기 이전이거나 basket corp_code 미매핑 가능"
    write_json(output_path, result)
    return result


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--root",  default=".")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    configured = bool(os.getenv("DART_API_KEY", "").strip())
    print(f"DART_KEY_CONFIGURED={str(configured).lower()}")
    result = collect(Path(args.root).resolve(), force=args.force)
    summary = {
        "status":     result.get("status"),
        "industries": len(result.get("industries", [])),
        "calls":      result.get("external_calls", 0),
        "quarter":    result.get("quarter"),
        "year":       result.get("reference_year"),
    }
    print(json.dumps(summary, ensure_ascii=False))
    if result.get("diagnostics"):
        for d in result["diagnostics"]:
            print(f"  {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
