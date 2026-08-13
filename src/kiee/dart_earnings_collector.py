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
MAX_CALLS        = 120     # corpCode ZIP 1회 + 회사당 2회 × 최대 3개사 × 17개 산업 = 103
CACHE_TTL_H      = 24
CORPCODE_TTL_H   = 168     # corp_code 맵은 1주일 캐시 (자주 안 바뀜)
MIN_BASKET_CNT   = 2
MAX_FIRMS        = 3       # 산업당 최대 조회 기업 수

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
    for item in root_elem.iter("list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        corp_code  = (item.findtext("corp_code")  or "").strip()
        if stock_code and corp_code:
            corp_map[stock_code] = corp_code

    write_json(cache_path, {"fetched_at": utc_now_iso(), "map": corp_map})
    return corp_map


# ── 분기 영업이익 조회 ────────────────────────────────────────────────────────

def _fetch_op(corp_code: str, year: int, reprt_code: str, api_key: str) -> float | None:
    """단일 기업 특정 분기 영업이익(원). CFS 우선, 없으면 OFS."""
    for fs_div in ("CFS", "OFS"):
        try:
            data = _get_json("fnlttSinglAcntAll.json", {
                "corp_code":  corp_code,
                "bsns_year":  str(year),
                "reprt_code": reprt_code,
                "fs_div":     fs_div,
            }, api_key)
            if data.get("status") not in ("000", None):
                continue
            for row in _rows(data):
                nm  = str(row.get("account_nm") or "").strip()
                aid = str(row.get("account_id") or "").strip()
                if nm in ("영업이익", "영업이익(손실)") or "ProfitLossFromOperating" in aid:
                    val = _number(row.get("thstrm_amount") or row.get("thstrm_add_amount"))
                    if val is not None:
                        return val
        except Exception:
            pass
    return None


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


def collect_industry(
    industry: dict[str, Any],
    api_key: str,
    corp_map: dict[str, str],
    call_count: list[int],
    max_calls: int,
    current_year: int,
    qcode: str,
    reprt_code: str,
) -> dict[str, Any] | None:
    basket = industry.get("krx_basket") or []
    if len(basket) < MIN_BASKET_CNT:
        return None

    # stock_code → corp_code 변환 (API 호출 없음)
    firms = []
    for sc in basket[:MAX_FIRMS * 2]:   # 여유분 확보
        cc = corp_map.get(str(sc).zfill(6)) or corp_map.get(str(sc))
        if cc:
            firms.append((sc, cc))
        if len(firms) >= MAX_FIRMS:
            break

    if not firms:
        return None

    yoy_list: list[float] = []
    n_fetched = 0

    for stock_code, corp_code in firms:
        if call_count[0] + 2 > max_calls:
            break

        # 당해 분기 OP
        call_count[0] += 1
        cur_op = _fetch_op(corp_code, current_year, reprt_code, api_key)
        time.sleep(0.12)

        # 전년 동 분기 OP
        call_count[0] += 1
        prev_year = current_year - 1
        # FY는 전년도, 나머지는 전년 동 분기
        prev_op = _fetch_op(corp_code, prev_year, reprt_code, api_key)
        time.sleep(0.12)

        if cur_op is not None and prev_op and prev_op != 0:
            yoy = (cur_op / abs(prev_op) - 1.0) * 100.0
            yoy_list.append(yoy)
            n_fetched += 1

    if not yoy_list:
        return None

    yoy_list.sort()
    median_yoy = yoy_list[len(yoy_list) // 2]
    pct_score  = clamp(50.0 + median_yoy / 100.0 * 50.0, 0.0, 100.0)
    score      = clamp(50.0 + (pct_score - 50.0) * SHRINKAGE, 0.0, 100.0)
    quality    = clamp(QUALITY_CAP * (0.5 + 0.5 * min(n_fetched / MAX_FIRMS, 1.0)), 0.0, QUALITY_CAP)
    surprise   = _surprise_flag(yoy_list)

    return {
        "id":                f"dart_earnings_{industry['key']}",
        "factor":            "earnings_momentum",
        "value":             round(median_yoy, 2),
        "unit":              "영업이익 YoY % (중앙값)",
        "long_run_percentile": round(pct_score, 3),
        "score":             round(score, 4),
        "quality":           round(quality, 1),
        "source":            f"DART 공시 {qcode} 영업이익 ({n_fetched}개사 중앙값)",
        "series_id":         f"dart_op_{industry['key']}",
        "as_of":             f"{current_year}-{qcode}",
        "available":         True,
        "is_dart_earnings":  True,
        "median_yoy_pct":    round(median_yoy, 2),
        "n_firms":           n_fetched,
        "surprise":          surprise,
        "note":              f"DART corpCode ZIP 매핑 → fnlttSinglAcntAll. 품질상한 {QUALITY_CAP}, shrinkage {SHRINKAGE}",
    }


# ── 메인 수집 ─────────────────────────────────────────────────────────────────

def collect(root: Path, force: bool = False) -> dict[str, Any]:
    output_path = root / OUTPUT_RAW
    api_key = os.getenv("DART_API_KEY", "").strip()

    def _pending(reason: str, calls: int = 0, diag: list[str] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": "1.0.0", "status": "pending",
            "generated_at_utc": utc_now_iso(), "industries": [],
            "collector": "dart-earnings-v2", "reason": reason,
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
            if age is not None and age < CACHE_TTL_H:
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

    # ── Step 1: corpCode ZIP 1회 다운로드 → 전체 매핑 ──────────────────────────
    corp_map = _load_corpcode_map(root, api_key, call_count)
    if not corp_map:
        return _pending("corpCode.xml 다운로드 실패 — DART_API_KEY 확인 필요", call_count[0])

    # ── Step 2: 산업별 수집 ───────────────────────────────────────────────────
    results: list[dict[str, Any]] = []
    diagnostics: list[str] = [
        f"corp_map_size={len(corp_map)} quarter={qcode} year={current_year} calls_used={call_count[0]}"
    ]

    for ind in industries:
        if call_count[0] >= MAX_CALLS:
            diagnostics.append("call cap reached")
            break
        key = str(ind.get("key") or "")
        if not key:
            continue
        try:
            metric = collect_industry(
                ind, api_key, corp_map, call_count, MAX_CALLS,
                current_year, qcode, reprt_code,
            )
            if metric:
                results.append({
                    "industry_key": key,
                    "current": {"metrics": [metric], "dart_earnings": True},
                })
                diagnostics.append(
                    f"{key}: score={metric['score']:.1f} yoy={metric['median_yoy_pct']}%"
                    f" n={metric['n_firms']} surprise={metric['surprise']}"
                )
            else:
                diagnostics.append(f"{key}: no earnings data (corp_map miss or API empty)")
        except Exception as e:
            diagnostics.append(f"{key}: error {str(e)[:80]}")

    result: dict[str, Any] = {
        "schema_version":       "1.0.0",
        "status":               "raw" if results else "pending",
        "generated_at_utc":     utc_now_iso(),
        "collector":            "dart-earnings-v2",
        "quarter":              qcode,
        "reference_year":       current_year,
        "industries":           results,
        "scored_industry_count": len(results),
        "external_calls":       call_count[0],
        "diagnostics":          diagnostics,
        "note": (
            "DART corpCode ZIP 1회 다운로드로 전체 corp_code 매핑 후 "
            "fnlttSinglAcntAll로 분기 영업이익 YoY 수집. earnings_momentum 팩터 보강용."
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
