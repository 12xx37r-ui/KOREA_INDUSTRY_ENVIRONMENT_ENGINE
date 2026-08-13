"""
customs_nowcast_collector.py
관세청_품목별 수출입실적(GW) API — XML 응답 파싱.

End Point : https://apis.data.go.kr/1220000/Itemtrade
Operation : /getItemtradeList
Format    : XML
Secret    : CUSTOMS_API_KEY (data.go.kr 일반 인증키)
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .util import age_hours, clamp, finite, read_json, utc_now_iso, write_json

BASE_URL       = "https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList"
OUTPUT_RAW     = "input/customs_nowcast_raw.json"
CACHE_TTL_H    = 12
QUALITY_CAP    = 65.0
SHRINKAGE      = 0.70
MAX_CALLS      = 20   # 30→20 (속도 우선)
MAX_INDUSTRIES = 15   # 앞 15개 산업만 수집
API_TIMEOUT    = 8    # 25→8초 (무응답 조기 탈출)


def _get_xml(url: str, params: dict[str, Any], timeout: int = API_TIMEOUT) -> ET.Element:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": "kiee-customs/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return ET.fromstring(resp.read())


def _xml_text(el: ET.Element | None, tag: str, default: str = "") -> str:
    if el is None:
        return default
    child = el.find(tag)
    return (child.text or "").strip() if child is not None else default


def _number(value: Any) -> float | None:
    try:
        n = float(str(value).replace(",", "").strip())
        return n if n == n and abs(n) < 1e18 else None
    except (TypeError, ValueError):
        return None


def _fetch_hs(api_key: str, hs_code: str, year: int, month: int, call_count: list[int]) -> list[dict]:
    """단일 HS코드·연월 조회. XML 파싱 후 행 리스트 반환."""
    if call_count[0] >= MAX_CALLS:
        return []
    params = {
        "serviceKey": api_key,
        "year": str(year),
        "month": f"{month:02d}",
        "hsCd": hs_code,
        "numOfRows": "100",
        "pageNo": "1",
    }
    call_count[0] += 1
    try:
        root = _get_xml(BASE_URL, params)
        items = root.findall(".//item")
        result = []
        for item in items:
            row = {child.tag: (child.text or "").strip() for child in item}
            result.append(row)
        return result
    except Exception:
        return []


def _normalize_hs(hs: str) -> str:
    """HS 코드를 6자리로 정규화 (관세청 API 요구사항)."""
    hs = hs.strip().replace(" ", "")
    if len(hs) < 6:
        hs = hs.ljust(6, "0")
    return hs[:10]  # 최대 10자리


def _build_series(api_key: str, hs_codes: list[str], call_count: list[int]) -> dict[str, dict[str, float]]:
    """직전 완성 월 + 전년 동월 2회 호출로 YoY 계산용 시계열 구성.
    관세청 API는 month 파라미터 필수이며 HS코드 6자리 이상 필요.
    """
    now = datetime.now(timezone.utc)
    # 직전 완성 월 (당월은 미집계 가능 → 1개월 전)
    if now.month == 1:
        ref_year, ref_month = now.year - 1, 12
    else:
        ref_year, ref_month = now.year, now.month - 1

    yearly: dict[str, dict[str, float]] = {}

    for hs_raw in hs_codes[:1]:  # HS 코드 1개만 (호출 최소화)
        hs = _normalize_hs(hs_raw)
        # 당해 기준월 + 전년 동월 각 1회
        for year, month in [(ref_year, ref_month), (ref_year - 1, ref_month)]:
            if call_count[0] >= MAX_CALLS:
                return yearly
            params = {
                "serviceKey": api_key,
                "year":       str(year),
                "month":      f"{month:02d}",
                "hsCd":       hs,
                "numOfRows":  "100",
                "pageNo":     "1",
            }
            call_count[0] += 1
            try:
                root_el = _get_xml(BASE_URL, params)
                items = root_el.findall(".//item")
                for item in items:
                    row = {child.tag: (child.text or "").strip() for child in item}
                    exp = _number(row.get("expAmt") or row.get("expDlr") or row.get("exportAmt"))
                    imp = _number(row.get("impAmt") or row.get("impDlr") or row.get("importAmt"))
                    yk = str(year)
                    if yk not in yearly:
                        yearly[yk] = {"exp": 0.0, "imp": 0.0}
                    if exp:
                        yearly[yk]["exp"] += exp
                    if imp:
                        yearly[yk]["imp"] += imp
            except Exception:
                continue
    return yearly


def _yoy_percentile(series: dict[str, dict[str, float]], kind: str) -> float | None:
    """연간 데이터 기준 YoY% → 0~100 점수로 변환.
    YoY > 0: 50 이상, YoY < 0: 50 미만.
    clamp: -50%~+50% → 0~100점.
    """
    years = sorted(series.keys())
    if len(years) < 2:
        return None
    cur = series[years[-1]].get(kind, 0.0)
    prv = series[years[-2]].get(kind, 0.0)
    if prv <= 0 or cur <= 0:
        return None
    yoy = (cur / prv - 1.0) * 100.0  # %
    # -50%~+50% 범위를 0~100으로 선형 매핑
    score = clamp(50.0 + yoy, 0.0, 100.0)
    return round(score, 3)


def collect(root: Path, force: bool = False) -> dict[str, Any]:
    output_path = root / OUTPUT_RAW
    api_key = os.getenv("CUSTOMS_API_KEY", "").strip()

    def _pending(reason: str) -> dict[str, Any]:
        result = {
            "schema_version": "1.0.0", "status": "pending",
            "generated_at_utc": utc_now_iso(), "industries": [],
            "collector": "customs-nowcast-v1", "reason": reason, "external_calls": 0,
        }
        write_json(output_path, result)
        return result

    if not api_key:
        return _pending("CUSTOMS_API_KEY 미설정")

    if not force:
        prev = read_json(output_path, {}) or {}
        if isinstance(prev, dict) and prev.get("status") in {"raw", "scored"}:
            if (age_hours(prev.get("generated_at_utc")) or 999) < CACHE_TTL_H:
                prev["cache_hit"] = True
                return prev

    mapping_path = root / "config" / "customs_hs_mapping.json"
    mapping = read_json(mapping_path, {}) or {}
    industry_configs = mapping.get("industries", {})
    if not industry_configs:
        return _pending("config/customs_hs_mapping.json 없음")

    call_count = [0]
    results: list[dict] = []
    diagnostics: list[str] = []

    for industry_key, ind_cfg in list(industry_configs.items())[:MAX_INDUSTRIES]:
        if call_count[0] >= MAX_CALLS:
            diagnostics.append("call cap reached")
            break
        hs_codes = ind_cfg.get("hs_codes", [])
        exp_w = float(ind_cfg.get("export_weight", 0.7))
        imp_w = float(ind_cfg.get("import_weight", 0.3))

        series = _build_series(api_key, hs_codes, call_count)
        if not series:
            diagnostics.append(f"{industry_key}: no data for HS {hs_codes[:2]}")
            continue

        exp_pct = _yoy_percentile(series, "exp")
        imp_pct = _yoy_percentile(series, "imp")
        if exp_pct is None and imp_pct is None:
            diagnostics.append(f"{industry_key}: insufficient series ({len(series)}개월)")
            continue

        pieces = []
        if exp_pct is not None:
            pieces.append((exp_pct, exp_w))
        if imp_pct is not None:
            pieces.append((imp_pct, imp_w))
        total_w = sum(w for _, w in pieces)
        raw_score = sum(s * w for s, w in pieces) / total_w
        score = clamp(50.0 + (raw_score - 50.0) * SHRINKAGE, 0.0, 100.0)

        as_of = sorted(series.keys())[-1]
        metric = {
            "id": f"customs_nowcast_{hs_codes[0] if hs_codes else 'unk'}",
            "factor": "production_shipments",
            "score": round(score, 4),
            "quality": QUALITY_CAP,
            "source": f"관세청 수출입실적 HS={','.join(hs_codes[:2])} 기준={as_of}",
            "as_of": f"{as_of[:4]}-{as_of[4:6]}-01",
            "available": True,
            "is_nowcast": True,
            "note": f"관세청 GW API. 품질상한 {QUALITY_CAP}, shrinkage {SHRINKAGE}",
        }
        results.append({
            "industry_key": industry_key,
            "current": {"metrics": [metric], "nowcast": True},
            "as_of": as_of,
        })
        diagnostics.append(f"{industry_key}: score={score:.1f} as_of={as_of}")

    result = {
        "schema_version": "1.0.0",
        "status": "raw" if results else "pending",
        "generated_at_utc": utc_now_iso(),
        "collector": "customs-nowcast-v1",
        "industries": results,
        "scored_industry_count": len(results),
        "external_calls": call_count[0],
        "diagnostics": diagnostics,
        "missing_data_policy": "do_not_impute_or_neutral_fill",
    }
    write_json(output_path, result)
    return result


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    print(f"CUSTOMS_KEY_CONFIGURED={'true' if os.getenv('CUSTOMS_API_KEY','').strip() else 'false'}")
    result = collect(Path(args.root).resolve(), force=args.force)
    print(json.dumps({
        "status": result.get("status"),
        "industries": len(result.get("industries", [])),
        "calls": result.get("external_calls", 0),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
