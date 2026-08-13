"""
dart_earnings_collector.py
DART 무료 API로 산업별 분기 영업이익 YoY를 수집해
earnings_momentum 팩터의 정밀도를 높인다.

사용 Secret: DART_API_KEY (opendart.fss.or.kr)
출력: input/dart_earnings_raw.json

approach:
  1. industry.krx_basket 종목코드로 DART corp_code 조회
  2. 최근 3개 분기 영업이익(OI) 조회
  3. YoY% 중앙값 → 장기 분포 백분위 → earnings_momentum metric
  4. 분기 서프라이즈(전분기 대비 급등/급락) 여부 플래그
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .util import age_hours, clamp, finite, read_json, utc_now_iso, write_json

DART_BASE      = "https://opendart.fss.or.kr/api"
OUTPUT_RAW     = "input/dart_earnings_raw.json"
QUALITY_CAP    = 72.0    # DART 공시 기반 — 실물지표보다 높은 품질 허용
SHRINKAGE      = 0.80
MAX_CALLS      = 60      # 산업당 최대 2~3회 호출
CACHE_TTL_H    = 24      # 분기 데이터는 하루 캐시
MIN_BASKET_CNT = 2       # 최소 2개 기업 이상 있어야 산업 신호 생성

# 분기 코드 → 공시 유형
_REPRT_CODE = {"1Q": "11013", "HY": "11012", "3Q": "11014", "FY": "11011"}


def _get(endpoint: str, params: dict[str, Any], api_key: str, timeout: int = 25) -> Any:
    params = dict(params)
    params["crtfc_key"] = api_key
    params["type"] = "json"
    url = f"{DART_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "kiee-dart-earnings/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


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


# ── corp_code 조회 ────────────────────────────────────────────────────────────

def _get_corp_code(stock_code: str, api_key: str) -> str | None:
    """주식 종목코드 → DART corp_code 변환."""
    try:
        data = _get("company.json", {"stock_code": stock_code}, api_key)
        return str(data.get("corp_code") or "").strip() or None
    except Exception:
        return None


# ── 분기 영업이익 조회 ────────────────────────────────────────────────────────

def _fetch_quarterly_op(
    corp_code: str,
    year: int,
    quarter_code: str,
    api_key: str,
) -> float | None:
    """단일 기업의 특정 분기 영업이익(원)을 반환."""
    reprt_code = _REPRT_CODE.get(quarter_code, "11013")
    try:
        data = _get("fnlttSinglAcntAll.json", {
            "corp_code": corp_code,
            "bsns_year": str(year),
            "reprt_code": reprt_code,
            "fs_div": "CFS",   # 연결 우선
        }, api_key)
        rows = _rows(data)
        # 영업이익 항목 탐색: account_nm이 '영업이익' 또는 account_id가 'ifrs-full_ProfitLossFromOperatingActivities'
        for row in rows:
            nm = str(row.get("account_nm") or "").strip()
            aid = str(row.get("account_id") or "").strip()
            if nm in ("영업이익", "영업이익(손실)") or "ProfitLossFromOperating" in aid:
                val = _number(row.get("thstrm_amount") or row.get("thstrm_add_amount"))
                if val is not None:
                    return val
        return None
    except Exception:
        return None


# ── 산업 실적 모멘텀 계산 ─────────────────────────────────────────────────────

def _op_yoy_percentile(yoy_series: list[float]) -> float | None:
    """YoY 시계열에서 최신 YoY의 장기 분포 백분위 (0~100) 반환."""
    if not yoy_series:
        return None
    latest = yoy_series[-1]
    rank = sum(1 for v in yoy_series if v <= latest) / len(yoy_series) * 100.0
    return round(rank, 3)


def _surprise_flag(yoy_series: list[float]) -> bool:
    """최근 분기 YoY가 직전 2분기 평균의 1.5배 이상 상승 → 서프라이즈."""
    if len(yoy_series) < 3:
        return False
    recent = yoy_series[-1]
    prior_avg = sum(yoy_series[-3:-1]) / 2
    return recent > prior_avg * 1.5 and recent > 0


def collect_industry(
    industry: dict[str, Any],
    api_key: str,
    call_count: list[int],
    max_calls: int,
    current_year: int,
) -> dict[str, Any] | None:
    """단일 산업의 분기 영업이익 신호 수집."""
    basket = industry.get("krx_basket") or []
    if len(basket) < MIN_BASKET_CNT:
        return None

    # 최근 4개 분기 정의 (단순화: 현재 연도 1Q~3Q + 전년 FY)
    quarters = [
        (current_year - 1, "FY"),
        (current_year,     "1Q"),
        (current_year,     "HY"),
        (current_year,     "3Q"),
    ]

    firm_yoy: dict[str, list[float]] = {}   # stock_code → [YoY%]

    for stock_code in basket[:5]:  # 산업당 최대 5개 기업
        if call_count[0] >= max_calls:
            break
        # corp_code 조회
        call_count[0] += 1
        corp_code = _get_corp_code(stock_code, api_key)
        if not corp_code:
            continue
        time.sleep(0.1)  # API rate limit 배려

        # 당해 + 전년 분기 OP 수집
        op_by_q: dict[str, float | None] = {}
        for year, qcode in quarters:
            if call_count[0] >= max_calls:
                break
            call_count[0] += 1
            op = _fetch_quarterly_op(corp_code, year, qcode, api_key)
            op_by_q[f"{year}_{qcode}"] = op
            time.sleep(0.15)

        # YoY 계산: 같은 분기 기준
        cur_fy  = op_by_q.get(f"{current_year}_FY") or op_by_q.get(f"{current_year}_3Q")
        prev_fy = op_by_q.get(f"{current_year - 1}_FY")
        if cur_fy is not None and prev_fy and prev_fy != 0:
            yoy = (cur_fy / abs(prev_fy) - 1.0) * 100.0
            firm_yoy.setdefault(stock_code, []).append(yoy)

        # HY YoY
        cur_hy  = op_by_q.get(f"{current_year}_HY")
        prev_hy = op_by_q.get(f"{current_year - 1}_HY")
        if cur_hy is not None and prev_hy and prev_hy != 0:
            yoy = (cur_hy / abs(prev_hy) - 1.0) * 100.0
            firm_yoy.setdefault(stock_code, []).append(yoy)

    if not firm_yoy:
        return None

    # 산업 수준: 기업별 최신 YoY 중앙값
    all_yoy = sorted([yoys[-1] for yoys in firm_yoy.values() if yoys])
    if not all_yoy:
        return None

    median_yoy = all_yoy[len(all_yoy) // 2]
    # 백분위 (정규화): -50%~+50% 범위를 0~100으로 매핑
    pct_score = clamp(50.0 + median_yoy / 100.0 * 50.0, 0.0, 100.0)
    # shrinkage 적용
    score = clamp(50.0 + (pct_score - 50.0) * SHRINKAGE, 0.0, 100.0)

    # 서프라이즈 감지 (기업 과반)
    surprise_count = sum(
        1 for yoys in firm_yoy.values()
        if _surprise_flag(yoys)
    )
    has_surprise = surprise_count >= len(firm_yoy) / 2

    # 샘플 수 기반 quality
    n_firms = len(firm_yoy)
    quality = clamp(QUALITY_CAP * (0.5 + 0.5 * min(n_firms / 5.0, 1.0)), 0.0, QUALITY_CAP)

    # 최신 기준 분기 탐색
    q_labels = ["3Q", "HY", "1Q", "FY"]
    as_of_quarter = next((q for q in q_labels if op_by_q.get(f"{current_year}_{q}") is not None), "FY") if 'op_by_q' in dir() else "FY"

    return {
        "id": f"dart_earnings_{industry['key']}",
        "factor": "earnings_momentum",
        "value": round(median_yoy, 2),
        "unit": "영업이익 YoY % (중앙값)",
        "long_run_percentile": round(pct_score, 3),
        "score": round(score, 4),
        "quality": round(quality, 1),
        "source": f"DART 공시 분기 OP ({n_firms}개사 중앙값 기준)",
        "series_id": f"dart_op_{industry['key']}",
        "as_of": f"{current_year}-{as_of_quarter}",
        "available": True,
        "is_dart_earnings": True,
        "median_yoy_pct": round(median_yoy, 2),
        "n_firms": n_firms,
        "surprise": has_surprise,
        "note": f"DART 무료 API 분기 영업이익 YoY. 품질상한 {QUALITY_CAP}, shrinkage {SHRINKAGE}",
    }


# ── 메인 수집 ─────────────────────────────────────────────────────────────────

def collect(root: Path, force: bool = False) -> dict[str, Any]:
    from datetime import datetime, timezone
    output_path = root / OUTPUT_RAW
    api_key = os.getenv("DART_API_KEY", "").strip()

    def _pending(reason: str) -> dict[str, Any]:
        result = {
            "schema_version": "1.0.0", "status": "pending",
            "generated_at_utc": utc_now_iso(), "industries": [],
            "collector": "dart-earnings-v1", "reason": reason, "external_calls": 0,
        }
        write_json(output_path, result)
        return result

    if not api_key:
        return _pending("DART_API_KEY 미설정. GitHub Secret에 DART_API_KEY를 추가하세요.")

    if not force:
        prev = read_json(output_path, {}) or {}
        if isinstance(prev, dict) and prev.get("status") in {"raw", "scored"}:
            data_age = age_hours(prev.get("generated_at_utc"))
            if data_age is not None and data_age < CACHE_TTL_H:
                prev["cache_hit"] = True
                return prev

    now = datetime.now(timezone.utc)
    current_year = now.year

    try:
        industries_raw = read_json(root / "config" / "industries.json", {})
        industries = (industries_raw or {}).get("industries") or []
    except Exception:
        return _pending("config/industries.json 로드 실패")

    call_count = [0]
    results: list[dict[str, Any]] = []
    diagnostics: list[str] = []

    for ind in industries:
        if call_count[0] >= MAX_CALLS:
            diagnostics.append("call cap reached")
            break
        key = str(ind.get("key") or "")
        if not key:
            continue
        try:
            metric = collect_industry(ind, api_key, call_count, MAX_CALLS, current_year)
            if metric:
                results.append({
                    "industry_key": key,
                    "current": {"metrics": [metric], "dart_earnings": True},
                })
                diagnostics.append(f"{key}: score={metric['score']:.1f} yoy={metric['median_yoy_pct']}% n={metric['n_firms']} surprise={metric['surprise']}")
            else:
                diagnostics.append(f"{key}: no earnings data")
        except Exception as e:
            diagnostics.append(f"{key}: error {str(e)[:60]}")

    result = {
        "schema_version": "1.0.0",
        "status": "raw" if results else "pending",
        "generated_at_utc": utc_now_iso(),
        "collector": "dart-earnings-v1",
        "industries": results,
        "scored_industry_count": len(results),
        "external_calls": call_count[0],
        "diagnostics": diagnostics,
        "note": "DART 공시 기반 산업 분기 영업이익 YoY 신호. earnings_momentum 팩터 보강용.",
    }
    write_json(output_path, result)
    return result


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    print(f"DART_KEY_CONFIGURED={'true' if os.getenv('DART_API_KEY','').strip() else 'false'}")
    result = collect(Path(args.root).resolve(), force=args.force)
    print(json.dumps({"status": result.get("status"), "industries": len(result.get("industries", [])), "calls": result.get("external_calls", 0)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
