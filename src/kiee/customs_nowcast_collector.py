"""
customs_nowcast_collector.py
관세청 HS 품목별 수출입 실적 API — XML 응답 파싱.

End Point : https://apis.data.go.kr/1220000/impExpHsItemList/getImpExpHsItemList
Format    : XML
Secret    : CUSTOMS_API_KEY (data.go.kr 일반 인증키)

파라미터:
  serviceKey : 인증키
  yyyyMm     : 조회 연월 (YYYYMM)
  hsCd       : HS 코드 (2~10자리)
  numOfRows  : 행수
  pageNo     : 페이지
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

from .util import age_hours, clamp, read_json, utc_now_iso, write_json

# 올바른 엔드포인트 (HS 코드 기반 수출입 실적)
BASE_URL       = "https://apis.data.go.kr/1220000/impExpHsItemList/getImpExpHsItemList"
OUTPUT_RAW     = "input/customs_nowcast_raw.json"
CACHE_TTL_H    = 12
QUALITY_CAP    = 65.0
SHRINKAGE      = 0.70
MAX_CALLS      = 20
MAX_INDUSTRIES = 15
API_TIMEOUT    = 10


def _get_xml(url: str, params: dict[str, Any], timeout: int = API_TIMEOUT) -> tuple[ET.Element, str]:
    """(파싱된 XML 루트, 원본 텍스트 앞 300자) 반환.
    serviceKey는 data.go.kr 발급 시 이미 URL 인코딩됨 → 별도 처리로 이중 인코딩 방지.
    """
    service_key = params.get("serviceKey", "")
    other = {k: v for k, v in params.items() if k != "serviceKey" and v is not None}
    query = f"serviceKey={service_key}&{urllib.parse.urlencode(other)}"
    req = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": "kiee-customs/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    text = raw.decode("utf-8", errors="replace")
    return ET.fromstring(text), text[:300]


def _number(value: Any) -> float | None:
    try:
        n = float(str(value).replace(",", "").strip())
        return n if n == n and abs(n) < 1e18 else None
    except (TypeError, ValueError):
        return None


def _normalize_hs(hs: str) -> str:
    """HS 코드 정규화 — 2자리 챕터도 그대로 허용, 앞뒤 공백 제거."""
    return hs.strip().replace(" ", "")


def _ref_months(now: datetime) -> tuple[tuple[int, int], tuple[int, int]]:
    """(당해 기준월, 전년 동월) 반환. 직전 완성 월 기준."""
    if now.month == 1:
        cur = (now.year - 1, 12)
    else:
        cur = (now.year, now.month - 1)
    prev = (cur[0] - 1, cur[1])
    return cur, prev


def _build_series(
    api_key: str,
    hs_codes: list[str],
    call_count: list[int],
    now: datetime,
    debug_info: list[str],
) -> dict[str, dict[str, float]]:
    """
    당해 기준월 + 전년 동월 각 1회 호출 → YoY 계산용 딕셔너리.
    키: "YYYY", 값: {"exp": float, "imp": float}
    """
    (cur_year, cur_month), (prev_year, prev_month) = _ref_months(now)
    yearly: dict[str, dict[str, float]] = {}

    for hs_raw in hs_codes[:1]:
        hs = _normalize_hs(hs_raw)
        for year, month in [(cur_year, cur_month), (prev_year, prev_month)]:
            if call_count[0] >= MAX_CALLS:
                return yearly
            params = {
                "serviceKey": api_key,
                "yyyyMm":     f"{year}{month:02d}",
                "hsCd":       hs,
                "numOfRows":  "100",
                "pageNo":     "1",
            }
            call_count[0] += 1
            try:
                root_el, raw_snippet = _get_xml(BASE_URL, params)
                items = root_el.findall(".//item")
                # 결과코드 및 총건수 추출 (디버그용)
                result_code = (root_el.findtext(".//resultCode") or "").strip()
                total_count = (root_el.findtext(".//totalCount") or "0").strip()
                debug_info.append(
                    f"HS={hs} {year}{month:02d}: code={result_code} total={total_count}"
                    f" items={len(items)} raw={raw_snippet[:120]!r}"
                )
                yk = str(year)
                if yk not in yearly:
                    yearly[yk] = {"exp": 0.0, "imp": 0.0}
                for item in items:
                    row = {c.tag: (c.text or "").strip() for c in item}
                    exp = _number(
                        row.get("expAmt") or row.get("expDlr") or
                        row.get("exportAmt") or row.get("exp") or
                        row.get("expWgt")
                    )
                    imp = _number(
                        row.get("impAmt") or row.get("impDlr") or
                        row.get("importAmt") or row.get("imp") or
                        row.get("impWgt")
                    )
                    if exp:
                        yearly[yk]["exp"] += exp
                    if imp:
                        yearly[yk]["imp"] += imp
            except Exception as e:
                debug_info.append(f"HS={hs} {year}{month:02d}: exception={str(e)[:80]}")
                continue
    return yearly


def _yoy_score(series: dict[str, dict[str, float]], kind: str) -> float | None:
    years = sorted(series.keys())
    if len(years) < 2:
        return None
    cur = series[years[-1]].get(kind, 0.0)
    prv = series[years[-2]].get(kind, 0.0)
    if prv <= 0 or cur <= 0:
        return None
    yoy = (cur / prv - 1.0) * 100.0
    return round(clamp(50.0 + yoy, 0.0, 100.0), 3)


def collect(root: Path, force: bool = False) -> dict[str, Any]:
    output_path = root / OUTPUT_RAW
    api_key = os.getenv("CUSTOMS_API_KEY", "").strip()

    def _pending(reason: str, calls: int = 0) -> dict[str, Any]:
        result = {
            "schema_version": "1.0.0", "status": "pending",
            "generated_at_utc": utc_now_iso(), "industries": [],
            "collector": "customs-nowcast-v2", "reason": reason,
            "external_calls": calls,
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

    now = datetime.now(timezone.utc)
    (cur_year, cur_month), _ = _ref_months(now)
    ref_label = f"{cur_year}{cur_month:02d}"

    call_count = [0]
    results: list[dict] = []
    diagnostics: list[str] = [f"endpoint=impExpHsItemList ref={ref_label}"]

    for industry_key, ind_cfg in list(industry_configs.items())[:MAX_INDUSTRIES]:
        if call_count[0] >= MAX_CALLS:
            diagnostics.append("call cap reached")
            break
        hs_codes = ind_cfg.get("hs_codes", [])
        exp_w = float(ind_cfg.get("export_weight", 0.7))
        imp_w = float(ind_cfg.get("import_weight", 0.3))

        series = _build_series(api_key, hs_codes, call_count, now, diagnostics)
        if not series:
            diagnostics.append(f"{industry_key}: no data HS={hs_codes[:2]}")
            continue

        exp_pct = _yoy_score(series, "exp")
        imp_pct = _yoy_score(series, "imp")
        if exp_pct is None and imp_pct is None:
            diagnostics.append(f"{industry_key}: series empty HS={hs_codes[:1]}")
            continue

        pieces = [(s, w) for s, w in [(exp_pct, exp_w), (imp_pct, imp_w)] if s is not None]
        total_w = sum(w for _, w in pieces)
        raw_score = sum(s * w for s, w in pieces) / total_w
        score = round(clamp(50.0 + (raw_score - 50.0) * SHRINKAGE, 0.0, 100.0), 4)

        metric = {
            "id":        f"customs_nowcast_{hs_codes[0] if hs_codes else 'unk'}",
            "factor":    "production_shipments",
            "score":     score,
            "quality":   QUALITY_CAP,
            "source":    f"관세청 HS={','.join(hs_codes[:2])} {ref_label}",
            "as_of":     f"{cur_year}-{cur_month:02d}-01",
            "available": True,
            "is_nowcast": True,
            "exp_yoy_score": exp_pct,
            "imp_yoy_score": imp_pct,
            "note": f"관세청 impExpHsItemList API. 품질상한 {QUALITY_CAP}, shrinkage {SHRINKAGE}",
        }
        results.append({
            "industry_key": industry_key,
            "current": {"metrics": [metric], "nowcast": True},
            "as_of": ref_label,
        })
        diagnostics.append(f"{industry_key}: score={score} exp={exp_pct} imp={imp_pct}")

    result = {
        "schema_version": "1.0.0",
        "status": "raw" if results else "pending",
        "generated_at_utc": utc_now_iso(),
        "collector": "customs-nowcast-v2",
        "reference_month": ref_label,
        "industries": results,
        "scored_industry_count": len(results),
        "external_calls": call_count[0],
        "diagnostics": diagnostics,
        "missing_data_policy": "do_not_impute_or_neutral_fill",
    }
    if not results:
        result["reason"] = "모든 HS 코드 조회 결과 없음 — API 응답 필드명 또는 HS 코드 확인 필요"
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
        "status":     result.get("status"),
        "industries": len(result.get("industries", [])),
        "calls":      result.get("external_calls", 0),
        "ref_month":  result.get("reference_month"),
    }, ensure_ascii=False))
    if result.get("diagnostics"):
        for d in result["diagnostics"]:
            print(f"  {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
