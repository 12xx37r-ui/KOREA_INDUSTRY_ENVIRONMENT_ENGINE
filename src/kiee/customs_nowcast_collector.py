"""
customs_nowcast_collector.py
관세청 HS 품목별 수출입 실적 API.

Secret : CUSTOMS_API_KEY (data.go.kr 일반 인증키)
출력   : input/customs_nowcast_raw.json

API 400/접근 오류 시 즉시 pending 처리 — 워크플로우 블로킹 방지.
성공 시: status=raw, industries 채워짐.
실패 시: status=pending, reason에 원인 기록 (엔진은 정상 동작).
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

# API 400 지속 발생 — 재활성화 전까지 수집 비활성화
# 원인: hsSgn 파라미터 규격/엔드포인트 불일치 또는 서비스 미등록
CUSTOMS_DISABLED = False

ENDPOINTS = [
    "https://apis.data.go.kr/1220000/impExpHsItemList/getImpExpHsItemList",
    "https://apis.data.go.kr/1220000/need2MainItemList/getNeed2MainItemList",
]
OUTPUT_RAW     = "input/customs_nowcast_raw.json"
CACHE_TTL_H    = 12
QUALITY_CAP    = 65.0
SHRINKAGE      = 0.70
MAX_CALLS      = 12    # 빠르게 실패 확인 후 종료
MAX_INDUSTRIES = 10
API_TIMEOUT    = 8
# 연속 400 오류 허용 횟수 — 초과 시 즉시 종료
MAX_CONSECUTIVE_ERRORS = 3


def _request(url: str, api_key: str, other_params: dict[str, Any], timeout: int = API_TIMEOUT) -> tuple[str, int]:
    """(응답 텍스트, HTTP 상태코드) 반환. serviceKey 이중 인코딩 방지."""
    # data.go.kr 키는 인코딩/디코딩 형태 모두 존재 — 디코딩 후 재인코딩으로 통일
    decoded_key = urllib.parse.unquote(api_key)
    encoded_key = urllib.parse.quote(decoded_key, safe="")
    rest = urllib.parse.urlencode({k: v for k, v in other_params.items() if v is not None})
    full_url = f"{url}?serviceKey={encoded_key}&{rest}"
    req = urllib.request.Request(full_url, headers={"User-Agent": "kiee-customs/3.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace"), resp.status
    except urllib.error.HTTPError as e:
        return "", e.code
    except Exception as e:
        return str(e), -1


def _number(value: Any) -> float | None:
    try:
        n = float(str(value).replace(",", "").strip())
        return n if n == n and abs(n) < 1e18 else None
    except (TypeError, ValueError):
        return None


def _ref_months(now: datetime) -> tuple[tuple[int, int], tuple[int, int]]:
    if now.month == 1:
        cur = (now.year - 1, 12)
    else:
        cur = (now.year, now.month - 1)
    return cur, (cur[0] - 1, cur[1])


def _probe_endpoint(api_key: str, diag: list[str]) -> tuple[str, dict[str, str]] | None:
    """
    동작하는 엔드포인트 + 파라미터 형식을 자동 탐색.
    성공하면 (endpoint_url, base_params_template) 반환, 실패하면 None.
    """
    test_hs = "8542310000"  # 10자리 HSK 코드 필수
    test_ym = "202607"
    # 시도할 파라미터 조합 — searchBseYm + hsSgn 이 정식 파라미터명
    param_variants = [
        {"searchBseYm": test_ym, "hsSgn": test_hs, "numOfRows": "5", "pageNo": "1"},
        {"searchBseYm": test_ym, "hsSgn": test_hs, "numOfRows": "5", "pageNo": "1", "_type": "xml"},
        {"yyyyMm": test_ym, "hsSgn": test_hs, "numOfRows": "5", "pageNo": "1"},
        {"yyyymm": test_ym, "hsSgn": test_hs, "numOfRows": "5", "pageNo": "1"},
        {"strtYymm": test_ym, "endYymm": test_ym, "hsSgn": test_hs, "numOfRows": "5", "pageNo": "1"},
        {"yyyyMm": test_ym, "hsCd": test_hs, "numOfRows": "5", "pageNo": "1"},
    ]
    for endpoint in ENDPOINTS:
        for params in param_variants:
            text, code = _request(endpoint, api_key, params)
            tag = f"probe {endpoint.split('/')[-1]} {list(params.keys())[:2]}"
            if code == 200:
                diag.append(f"{tag}: OK code=200 snippet={text[:80]!r}")
                return endpoint, params
            else:
                diag.append(f"{tag}: code={code}")
    return None


def collect(root: Path, force: bool = False) -> dict[str, Any]:
    output_path = root / OUTPUT_RAW
    api_key = os.getenv("CUSTOMS_API_KEY", "").strip()

    def _pending(reason: str, calls: int = 0, diag: list[str] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": "1.0.0", "status": "pending",
            "generated_at_utc": utc_now_iso(), "industries": [],
            "collector": "customs-nowcast-v3", "reason": reason,
            "external_calls": calls,
        }
        if diag:
            result["diagnostics"] = diag
        write_json(output_path, result)
        return result

    if CUSTOMS_DISABLED:
        return _pending("관세청 API 일시 비활성화 — hsSgn 파라미터 규격 확인 후 재활성화 예정", 0)

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
    (cur_year, cur_month), (prev_year, prev_month) = _ref_months(now)
    ref_label = f"{cur_year}{cur_month:02d}"

    diag: list[str] = [f"ref={ref_label}"]
    call_count = [0]

    # ── 엔드포인트 자동 탐색 ──────────────────────────────────────────────────
    probe = _probe_endpoint(api_key, diag)
    call_count[0] += len(ENDPOINTS) * 6  # probe 호출 수 근사
    if probe is None:
        return _pending(
            "관세청 API 엔드포인트 탐색 실패 (400/인증 오류) — "
            "CUSTOMS_API_KEY가 이 서비스에 등록됐는지 확인 필요",
            call_count[0], diag
        )

    endpoint, base_params = probe
    diag.append(f"endpoint_ok={endpoint.split('/')[-1]} params={list(base_params.keys())}")

    # ── 산업별 수집 ───────────────────────────────────────────────────────────
    results: list[dict] = []

    for industry_key, ind_cfg in list(industry_configs.items())[:MAX_INDUSTRIES]:
        if call_count[0] >= MAX_CALLS:
            diag.append("call cap reached")
            break
        hs_codes = ind_cfg.get("hs_codes", [])
        if not hs_codes:
            continue
        hs = hs_codes[0]
        exp_w = float(ind_cfg.get("export_weight", 0.7))
        imp_w = float(ind_cfg.get("import_weight", 0.3))

        yearly: dict[str, dict[str, float]] = {}
        for year, month in [(cur_year, cur_month), (prev_year, prev_month)]:
            if call_count[0] >= MAX_CALLS:
                break
            params = dict(base_params)
            ym = f"{year}{month:02d}"
            if "searchBseYm" in params:
                params["searchBseYm"] = ym
            elif "yyyyMm" in params:
                params["yyyyMm"] = ym
            elif "yyyymm" in params:
                params["yyyymm"] = ym
            elif "strtYymm" in params:
                params["strtYymm"] = ym
                params["endYymm"] = ym
            else:
                params["year"] = str(year)
                params["month"] = f"{month:02d}"
            for hk in ("hsSgn", "hsCd", "hscd"):
                if hk in params:
                    params[hk] = hs
                    break

            call_count[0] += 1
            text, code = _request(endpoint, api_key, params)
            if code != 200:
                diag.append(f"{industry_key} {year}{month:02d}: HTTP {code}")
                continue
            try:
                root_el = ET.fromstring(text)
                items = root_el.findall(".//item")
                yk = str(year)
                yearly.setdefault(yk, {"exp": 0.0, "imp": 0.0})
                for item in items:
                    row = {c.tag: (c.text or "").strip() for c in item}
                    exp = _number(row.get("expAmt") or row.get("expDlr") or row.get("exportAmt"))
                    imp = _number(row.get("impAmt") or row.get("impDlr") or row.get("importAmt"))
                    if exp:
                        yearly[yk]["exp"] += exp
                    if imp:
                        yearly[yk]["imp"] += imp
            except Exception as e:
                diag.append(f"{industry_key} parse error: {str(e)[:60]}")

        years = sorted(yearly.keys())
        if len(years) < 2:
            diag.append(f"{industry_key}: series<2 HS={hs}")
            continue

        def yoy_score(kind: str) -> float | None:
            cur = yearly[years[-1]].get(kind, 0.0)
            prv = yearly[years[-2]].get(kind, 0.0)
            if prv <= 0 or cur <= 0:
                return None
            return round(clamp(50.0 + (cur / prv - 1.0) * 100.0, 0.0, 100.0), 3)

        exp_pct = yoy_score("exp")
        imp_pct = yoy_score("imp")
        if exp_pct is None and imp_pct is None:
            diag.append(f"{industry_key}: no positive values")
            continue

        pieces = [(s, w) for s, w in [(exp_pct, exp_w), (imp_pct, imp_w)] if s is not None]
        raw_score = sum(s * w for s, w in pieces) / sum(w for _, w in pieces)
        score = round(clamp(50.0 + (raw_score - 50.0) * SHRINKAGE, 0.0, 100.0), 4)

        results.append({
            "industry_key": industry_key,
            "current": {"metrics": [{
                "id":            f"customs_nowcast_{hs}",
                "factor":        "production_shipments",
                "score":         score,
                "quality":       QUALITY_CAP,
                "source":        f"관세청 HS={hs} {ref_label}",
                "as_of":         f"{cur_year}-{cur_month:02d}-01",
                "available":     True,
                "is_nowcast":    True,
                "exp_yoy_score": exp_pct,
                "imp_yoy_score": imp_pct,
                "note":          f"관세청 API. 품질상한 {QUALITY_CAP}, shrinkage {SHRINKAGE}",
            }], "nowcast": True},
            "as_of": ref_label,
        })
        diag.append(f"{industry_key}: score={score} exp={exp_pct} imp={imp_pct}")

    result: dict[str, Any] = {
        "schema_version": "1.0.0",
        "status": "raw" if results else "pending",
        "generated_at_utc": utc_now_iso(),
        "collector": "customs-nowcast-v3",
        "reference_month": ref_label,
        "industries": results,
        "scored_industry_count": len(results),
        "external_calls": call_count[0],
        "diagnostics": diag,
        "missing_data_policy": "do_not_impute_or_neutral_fill",
    }
    if not results:
        result["reason"] = "수집 데이터 없음 — diagnostics 참조"
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
    for d in (result.get("diagnostics") or []):
        print(f"  {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
