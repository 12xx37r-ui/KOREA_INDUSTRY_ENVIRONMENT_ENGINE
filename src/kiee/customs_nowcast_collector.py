"""
customs_nowcast_collector.py
관세청 품목별 수출입 실적(GW) API를 이용한 nowcasting 수집기.

KOSIS 실물지표의 1~2개월 발표 시차를 메우기 위해
수출입 YoY 속보치를 보조 nowcasting 지표로 제공한다.

Secret 이름: CUSTOMS_API_KEY
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .util import age_hours, clamp, finite, read_json, utc_now_iso, write_json

# ── 상수 ─────────────────────────────────────────────────────────────────────
BASE_URL       = "https://apis.data.go.kr/1220000/need2MainItemList/getNeed2MainItemList"
HS_DETAIL_URL  = "https://apis.data.go.kr/1220000/impExpHsItemList/getImpExpHsItemList"
OUTPUT_RAW     = "input/customs_nowcast_raw.json"
CACHE_TTL_H    = 12          # 속보치이므로 12시간 캐시
QUALITY_CAP    = 65.0        # 수출입 데이터는 실물 직접지표 대용치 — quality 상한
SHRINKAGE      = 0.70        # 50점 방향 수축 계수
MAX_CALLS      = 24


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _get_json(url: str, params: dict[str, Any], timeout: int = 20) -> Any:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": "kiee-customs-nowcast/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("items", "item", "result", "data"):
            val = payload.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
            if isinstance(val, dict):
                inner = val.get("item") or val.get("items") or []
                if isinstance(inner, list):
                    return [r for r in inner if isinstance(r, dict)]
                if isinstance(inner, dict):
                    return [inner]
    return []


# ── 데이터 파싱 ───────────────────────────────────────────────────────────────

def _number(value: Any) -> float | None:
    try:
        n = float(str(value).replace(",", "").strip())
        return n if n == n and abs(n) < 1e15 else None
    except (TypeError, ValueError):
        return None


def _fetch_hs_monthly(api_key: str, hs_code: str, year: int, call_count: list[int], max_calls: int) -> list[dict[str, Any]]:
    """특정 HS 코드의 월별 수출입 실적 조회 (최근 14개월)."""
    if call_count[0] >= max_calls:
        return []
    params = {
        "serviceKey": api_key,
        "type": "json",
        "hsCode": hs_code,
        "year": str(year),
        "numOfRows": "100",
        "pageNo": "1",
    }
    call_count[0] += 1
    try:
        data = _get_json(HS_DETAIL_URL, params)
        return _rows(data)
    except Exception:
        return []


def _fetch_main_items(api_key: str, year: int, call_count: list[int], max_calls: int) -> list[dict[str, Any]]:
    """주요 품목별 연간 수출입 실적 (HS 2단위)."""
    if call_count[0] >= max_calls:
        return []
    params = {
        "serviceKey": api_key,
        "type": "json",
        "year": str(year),
        "numOfRows": "500",
        "pageNo": "1",
    }
    call_count[0] += 1
    try:
        data = _get_json(BASE_URL, params)
        return _rows(data)
    except Exception:
        return []


def _extract_monthly_series(rows: list[dict[str, Any]], hs_prefix: str) -> dict[str, float]:
    """
    API 응답에서 HS 코드에 해당하는 월별 수출액/수입액 합계 추출.
    반환: {"YYYYMM": export_usd, ...}
    """
    monthly: dict[str, dict[str, float]] = {}
    for row in rows:
        # HS 코드 필드 탐색
        hs = str(row.get("hsCode") or row.get("hsCd") or row.get("itemCd") or "").strip()
        if not hs.startswith(hs_prefix):
            continue
        # 기간 필드 탐색
        period = str(row.get("period") or row.get("yyyyMm") or row.get("date") or "").strip().replace("-", "")
        if len(period) < 6:
            continue
        ym = period[:6]
        exp_val = _number(row.get("expDlr") or row.get("exportAmt") or row.get("export") or 0)
        imp_val = _number(row.get("impDlr") or row.get("importAmt") or row.get("import") or 0)
        if ym not in monthly:
            monthly[ym] = {"exp": 0.0, "imp": 0.0}
        if exp_val:
            monthly[ym]["exp"] += exp_val
        if imp_val:
            monthly[ym]["imp"] += imp_val
    return monthly


def _yoy_percentile(series: dict[str, float], kind: str = "exp") -> float | None:
    """
    수출(exp) 또는 수입(imp) YoY% 를 장기 분포에서의 백분위(0~100)로 변환.
    YoY: (현재월 / 12개월 전) - 1
    백분위: 과거 series 내 YoY 분포에서의 위치
    """
    sorted_months = sorted(series.keys())
    if len(sorted_months) < 14:
        return None

    yoy_values: list[float] = []
    for i in range(12, len(sorted_months)):
        cur = series[sorted_months[i]].get(kind, 0.0) or 0.0
        prev = series[sorted_months[i - 12]].get(kind, 0.0) or 0.0
        if prev > 0:
            yoy_values.append((cur / prev - 1.0) * 100.0)

    if not yoy_values:
        return None

    latest_yoy = yoy_values[-1]
    # 백분위 계산
    rank = sum(1 for v in yoy_values if v <= latest_yoy) / len(yoy_values) * 100.0
    return round(rank, 4)


def _build_nowcast_metric(
    monthly_series: dict[str, dict[str, float]],
    hs_code: str,
    industry_key: str,
    export_weight: float,
    import_weight: float,
    as_of: str,
    source: str,
) -> dict[str, Any] | None:
    """
    월별 시계열에서 nowcasting metric을 생성.
    반환: scoring.py가 소비하는 metric dict
    """
    if len(monthly_series) < 14:
        return None

    exp_pct = _yoy_percentile(monthly_series, "exp")
    imp_pct = _yoy_percentile(monthly_series, "imp")

    if exp_pct is None and imp_pct is None:
        return None

    # 수출/수입 가중 합산 (수입은 산업별 특성에 따라 방향 해석)
    pieces = []
    if exp_pct is not None:
        pieces.append((exp_pct, export_weight))
    if imp_pct is not None:
        # 원자재 수입 의존 산업: 수입 증가 = 수요 호조 → 그대로 사용
        # 수출 위주 산업: 수입 감소 = 원가 부담 완화 → 역방향
        # import_weight > 0이면 그대로 활용 (산업별 설정에 위임)
        pieces.append((imp_pct, import_weight))

    if not pieces:
        return None

    total_w = sum(w for _, w in pieces)
    score = sum(s * w for s, w in pieces) / total_w
    score_shrunk = clamp(50.0 + (score - 50.0) * SHRINKAGE, 0.0, 100.0)

    # 최신 YoY 수치 계산 (표시용)
    sorted_months = sorted(monthly_series.keys())
    latest_ym = sorted_months[-1]
    prev12_ym = sorted_months[-13] if len(sorted_months) >= 13 else None
    latest_exp = monthly_series[latest_ym].get("exp", 0.0)
    prev_exp   = monthly_series.get(prev12_ym, {}).get("exp", 0.0) if prev12_ym else 0.0
    exp_yoy = round((latest_exp / prev_exp - 1.0) * 100.0, 2) if prev_exp > 0 else None

    return {
        "id": f"customs_nowcast_{hs_code}",
        "factor": "production_shipments",   # 수출 속보는 production_shipments 팩터로 연결
        "value": round(score_shrunk, 4),
        "unit": "수출 YoY 백분위",
        "long_run_percentile": round(score_shrunk, 4),
        "score": round(score_shrunk, 4),
        "quality": QUALITY_CAP,
        "source": source,
        "series_id": f"customs_hs_{hs_code}",
        "as_of": f"{as_of[:4]}-{as_of[4:6]}-01" if len(as_of) >= 6 else as_of,
        "available": True,
        "is_nowcast": True,
        "hs_code": hs_code,
        "latest_export_yoy_pct": exp_yoy,
        "sample_months": len(monthly_series),
        "note": f"관세청 수출입 속보 — 실물지표 lag 보완. 품질상한 {QUALITY_CAP}, shrinkage {SHRINKAGE}",
    }


# ── 메인 수집 로직 ────────────────────────────────────────────────────────────

def collect(root: Path, force: bool = False) -> dict[str, Any]:
    output_path = root / OUTPUT_RAW
    api_key = os.getenv("CUSTOMS_API_KEY", "").strip()
    mapping_path = root / "config" / "customs_hs_mapping.json"
    mapping = read_json(mapping_path, {}) or {}

    def _pending(reason: str) -> dict[str, Any]:
        result = {
            "schema_version": "1.0.0",
            "status": "pending",
            "generated_at_utc": utc_now_iso(),
            "industries": [],
            "collector": "customs-nowcast-v1",
            "reason": reason,
            "external_calls": 0,
        }
        write_json(output_path, result)
        return result

    if not api_key:
        return _pending("CUSTOMS_API_KEY가 설정되지 않았습니다. GitHub Secret에 CUSTOMS_API_KEY를 추가하세요.")

    if not isinstance(mapping, dict) or not mapping.get("industries"):
        return _pending("config/customs_hs_mapping.json이 없거나 비어 있습니다.")

    # 캐시 확인
    if not force:
        prev = read_json(output_path, {}) or {}
        if isinstance(prev, dict) and prev.get("status") in {"raw", "scored"}:
            data_age = age_hours(prev.get("generated_at_utc"))
            ttl = float(mapping.get("api", {}).get("cache_ttl_hours", CACHE_TTL_H))
            if data_age is not None and data_age < ttl:
                prev["cache_hit"] = True
                return prev

    now = datetime.now(timezone.utc)
    cur_year = now.year
    prev_year = cur_year - 1

    call_count = [0]
    max_calls = int(mapping.get("api", {}).get("max_external_calls", MAX_CALLS))
    diagnostics: list[str] = []
    by_industry: dict[str, dict[str, Any]] = {}

    # 각 산업별 HS 코드 수집
    industry_configs = mapping.get("industries", {})
    for industry_key, ind_cfg in industry_configs.items():
        if call_count[0] >= max_calls:
            diagnostics.append("call cap reached before remaining industries")
            break
        hs_codes = ind_cfg.get("hs_codes", [])
        exp_w = float(ind_cfg.get("export_weight", 0.7))
        imp_w = float(ind_cfg.get("import_weight", 0.3))

        # 산업별 월별 시계열 합산 (여러 HS 코드 합산)
        combined_monthly: dict[str, dict[str, float]] = {}
        for hs in hs_codes[:4]:  # 산업당 최대 4개 HS 코드
            if call_count[0] >= max_calls:
                break
            # 당해년도 + 전년도 조회
            for year in (cur_year, prev_year):
                if call_count[0] >= max_calls:
                    break
                rows = _fetch_hs_monthly(api_key, hs, year, call_count, max_calls)
                if rows:
                    monthly = _extract_monthly_series(rows, hs)
                    for ym, vals in monthly.items():
                        if ym not in combined_monthly:
                            combined_monthly[ym] = {"exp": 0.0, "imp": 0.0}
                        combined_monthly[ym]["exp"] += vals.get("exp", 0.0)
                        combined_monthly[ym]["imp"] += vals.get("imp", 0.0)

        if not combined_monthly:
            diagnostics.append(f"{industry_key}: no data returned for HS {hs_codes}")
            continue

        # 최신 기준월 결정
        as_of = sorted(combined_monthly.keys())[-1] if combined_monthly else ""
        source = f"관세청 수출입 실적 HS={','.join(hs_codes[:4])} 기준={as_of}"

        # 주 HS 코드 기준 metric 생성
        metric = _build_nowcast_metric(
            combined_monthly, hs_codes[0] if hs_codes else "unknown",
            industry_key, exp_w, imp_w, as_of, source,
        )
        if metric:
            by_industry[industry_key] = {
                "industry_key": industry_key,
                "current": {
                    "metrics": [metric],
                    "nowcast": True,
                },
                "as_of": as_of,
                "hs_codes": hs_codes,
            }
            diagnostics.append(f"{industry_key}: score={metric['score']:.1f} yoy={metric.get('latest_export_yoy_pct')}% as_of={as_of}")
        else:
            diagnostics.append(f"{industry_key}: insufficient series ({len(combined_monthly)} months)")

    result = {
        "schema_version": "1.0.0",
        "status": "raw" if by_industry else "pending",
        "generated_at_utc": utc_now_iso(),
        "collector": "customs-nowcast-v1",
        "industries": list(by_industry.values()),
        "scored_industry_count": len(by_industry),
        "external_calls": call_count[0],
        "diagnostics": diagnostics,
        "missing_data_policy": "do_not_impute_or_neutral_fill",
        "note": "관세청 수출입 속보치 nowcasting. KOSIS 실물지표 1~2개월 시차 보완용 보조 지표.",
    }
    write_json(output_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="관세청 수출입 nowcasting 수집기")
    parser.add_argument("--root", default=".")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    print(f"CUSTOMS_KEY_CONFIGURED={'true' if os.getenv('CUSTOMS_API_KEY', '').strip() else 'false'}")
    result = collect(Path(args.root).resolve(), force=args.force)
    print(json.dumps({
        "status": result.get("status"),
        "industry_count": len(result.get("industries", [])),
        "external_calls": result.get("external_calls", 0),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
