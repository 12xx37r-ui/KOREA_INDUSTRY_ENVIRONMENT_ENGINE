from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import load_all
from .util import read_json, utc_now_iso, write_json

# Search is available on the SSO host, while the official parameter-data and
# metadata examples use the main KOSIS host. Keep search and data endpoints
# separate; using the SSO host for parameter data can return empty rows.
SEARCH_URL = "https://sso.kosis.kr/openapi/statisticsSearch.do"
META_URL = "https://kosis.kr/openapi/statisticsData.do"
DATA_URL = "https://kosis.kr/openapi/Param/statisticsParameterData.do"

DEFAULT_TABLE_TITLE_HINTS = {
    "production_shipments": ("산업생산", "생산지수", "출하지수"),
    "inventory_cycle": ("재고", "재고율"),
    "utilization": ("가동률", "설비가동"),
    "pmi_bsi": ("경기실사지수", "BSI", "기업경기"),
}


def _number(value: Any) -> float | None:
    try:
        number = float(str(value).replace(",", "").strip())
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError):
        return None


@dataclass
class _CallBudget:
    limit: int
    attempts: int = 0
    scope_limit: int | None = None
    scope_attempts: int = 0
    errors: list[str] | None = None
    events: list[str] | None = None

    def start_scope(self, limit: int) -> None:
        self.scope_limit = max(1, int(limit))
        self.scope_attempts = 0

    def allow(self) -> None:
        if self.attempts >= self.limit:
            raise RuntimeError("KOSIS external-call cap reached")
        if self.scope_limit is not None and self.scope_attempts >= self.scope_limit:
            raise RuntimeError("KOSIS series-call cap reached")
        self.attempts += 1
        self.scope_attempts += 1


def _get_json(url: str, params: dict[str, Any], budget: _CallBudget, timeout: int = 25) -> Any:
    query = urllib.parse.urlencode({key: value for key, value in params.items() if value not in (None, "")})
    request = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": "kiee-industry-cycle/1.0"})
    budget.allow()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        if budget.errors is not None:
            table = params.get("tblId") or params.get("searchNm") or ""
            budget.errors.append(f"{url.rsplit('/', 1)[-1]}[{table}]: {type(exc).__name__}: {exc}")
        raise


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        if payload.get("err"):
            raise RuntimeError(f"KOSIS error {payload.get('err')}: {payload.get('errMsg', '')}")
        for key in ("result", "data", "rows"):
            if isinstance(payload.get(key), list):
                return [row for row in payload[key] if isinstance(row, dict)]
    return []


def _search(api_key: str, term: str, budget: _CallBudget) -> list[dict[str, Any]]:
    return _rows(_get_json(SEARCH_URL, {
        "method": "getList", "apiKey": api_key, "searchNm": term,
        "sort": "RANK", "startCount": 1, "resultCount": 20,
        "format": "json", "jsonVD": "Y",
    }, budget))


def _table_name(row: dict[str, Any]) -> str:
    for key in ("TBL_NM", "tblNm", "tableName", "STAT_NM", "statNm"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _meta_codes(api_key: str, org_id: str, table_id: str, budget: _CallBudget) -> tuple[str, str]:
    rows = _rows(_get_json(META_URL, {
        "method": "getMeta", "type": "ITM", "apiKey": api_key,
        "orgId": org_id, "tblId": table_id, "format": "json", "jsonVD": "Y",
    }, budget))
    obj_ids: list[str] = []
    itm_ids: list[str] = []
    for row in rows:
        obj_id = str(row.get("objId") or row.get("OBJ_ID") or "").strip()
        itm_id = str(row.get("itmId") or row.get("ITM_ID") or "").strip()
        if obj_id and obj_id not in obj_ids:
            obj_ids.append(obj_id)
        if itm_id and itm_id not in itm_ids:
            itm_ids.append(itm_id)
    return " ".join(obj_ids) or "ALL", " ".join(itm_ids) or "ALL"


def _meta_period(api_key: str, org_id: str, table_id: str, budget: _CallBudget) -> str:
    rows = _rows(_get_json(META_URL, {
        "method": "getMeta", "type": "PRD", "apiKey": api_key,
        "orgId": org_id, "tblId": table_id, "format": "json", "jsonVD": "Y",
    }, budget))
    for row in rows:
        value = str(row.get("PRD_SE") or row.get("prdSe") or "").strip().upper()
        if value in {"M", "Q", "Y", "H", "A"}:
            return value
    return "M"


def _fetch_table(api_key: str, org_id: str, table_id: str, periods: int, budget: _CallBudget) -> list[dict[str, Any]]:
    # KOSIS ITM metadata's objId is the classifier identifier, not a valid
    # objL1 selector value. Sending that identifier as objL1 produces empty
    # tables (or error 20/21) for otherwise valid tables. The official sample
    # query uses ALL for the first classifier and item, so use that first and
    # reserve selector retries for tables that explicitly require them.
    period = _meta_period(api_key, org_id, table_id, budget)
    base_params = {
        "method": "getList", "apiKey": api_key, "orgId": org_id, "tblId": table_id,
        "prdSe": period, "newEstPrdCnt": periods, "format": "json", "jsonVD": "Y",
    }
    selector_variants = (("ALL", "ALL"),)
    last_error: Exception | None = None
    for variant_index, (obj_selector, itm_selector) in enumerate(selector_variants):
        params = dict(base_params)
        params["itmId"] = itm_selector
        params["objL1"] = obj_selector
        for depth in range(0, 4):
            probe_params = dict(params)
            for level in range(2, 2 + depth):
                probe_params[f"objL{level}"] = "ALL"
            try:
                rows = _rows(_get_json(DATA_URL, probe_params, budget, timeout=25))
                if rows:
                    if budget.events is not None and depth:
                        budget.events.append(f"table_probe_selector: {table_id} ALL obj_depth={depth}")
                    return rows
            except RuntimeError as exc:
                last_error = exc
                text = str(exc)
                if "KOSIS error 20" not in text and "KOSIS error 21" not in text:
                    raise
                continue
    if last_error is not None:
        raise last_error
    return []


def _choose_table(api_key: str, spec: dict[str, Any], budget: _CallBudget) -> tuple[str, str, list[dict[str, Any]]]:
    periods = min(int(spec.get("periods", 24)), 24)
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    terms = list(spec.get("search_terms") or [])[:1]
    for term in terms:
        try:
            search_rows = _search(api_key, str(term), budget)
        except Exception:
            continue
        for row in search_rows:
            org = str(row.get("ORG_ID") or "").strip()
            table = str(row.get("TBL_ID") or "").strip()
            if org and table:
                candidates[(org, table)] = row
    if budget.events is not None:
        budget.events.append(f"search[{terms[0] if terms else ''}]: candidates={len(candidates)}")
    preferred = {
        str(table).strip(): index
        for index, table in enumerate(spec.get("preferred_tables") or [])
        if str(table).strip()
    }
    hints = tuple(str(value) for value in (spec.get("table_title_hints") or DEFAULT_TABLE_TITLE_HINTS.get(str(spec.get("factor") or ""), ())) if str(value))
    title_matches = [
        item for item in candidates.items()
        if not hints or any(hint in _table_name(item[1]) for hint in hints)
    ]
    ordered_candidates = title_matches or list(candidates.items())
    if budget.events is not None:
        preview = " | ".join(f"{table}:{_table_name(row)[:60]}" for (org, table), row in list(candidates.items())[:8])
        budget.events.append(f"search_titles: {preview}")
    ordered_candidates.sort(key=lambda item: (0 if item[0][1] in preferred else 1, preferred.get(item[0][1], 999)))
    probe_limit = max(1, int(spec.get("probe_candidates", 2)))
    probed: list[str] = []
    probe_errors: list[str] = []
    for (org, table), _ in ordered_candidates[:probe_limit]:
        probed.append(table)
        try:
            rows = _fetch_table(api_key, org, table, periods, budget)
            if rows:
                return org, table, rows
        except Exception as exc:
            probe_errors.append(f"{table}: {type(exc).__name__}: {exc}")
            continue
    if budget.events is not None and candidates:
        budget.events.append(f"table_probe: no usable rows for {','.join(probed)}")
        budget.events.extend(f"table_probe_error: {error}" for error in probe_errors)
    raise RuntimeError("no usable KOSIS table found")


def _label(row: dict[str, Any]) -> str:
    parts = []
    for key in ("C1_NM", "C2_NM", "C3_NM", "C4_NM", "ITM_NM", "TBL_NM"):
        value = str(row.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return " ".join(parts)


def _series_by_label(rows: list[dict[str, Any]], keywords: list[str]) -> dict[str, list[tuple[str, float]]]:
    grouped: dict[str, list[tuple[str, float]]] = {}
    for row in rows:
        label = _label(row)
        if not any(keyword in label for keyword in keywords):
            continue
        value = _number(row.get("DT"))
        period = str(row.get("PRD_DE") or "").strip()
        if value is None or not period:
            continue
        key = "|".join(str(row.get(f"C{i}") or row.get(f"C{i}_NM") or "") for i in range(1, 5))
        grouped.setdefault(key, []).append((period, value))
    return grouped


def _metric(series: list[tuple[str, float]], factor: str, source: str) -> dict[str, Any] | None:
    series = sorted(series, key=lambda item: item[0])
    if len(series) < 7:
        return None
    values = [value for _, value in series]
    latest_period, latest = series[-1]
    def change(months: int) -> float | None:
        if len(values) <= months or values[-1 - months] == 0:
            return None
        return round((latest / values[-1 - months] - 1.0) * 100.0, 4)
    rank = sum(1 for value in values if value <= latest) / len(values) * 100.0
    return {
        "id": factor, "factor": factor, "value": latest, "unit": "KOSIS index",
        "change_1m": change(1), "change_3m": change(3), "change_6m": change(6),
        "long_run_percentile": round(rank, 4), "quality": 85.0,
        "source": source, "series_id": factor, "as_of": f"{latest_period[:4]}-{latest_period[4:6]}-01",
    }


def collect(root: Path) -> dict[str, Any]:
    _, _, _ = load_all(root)
    api_key = os.getenv("KOSIS_API_KEY", "").strip()
    output = root / "input" / "industry_cycle_raw.json"
    if not api_key:
        result = {"schema_version": "1.0.0", "status": "pending", "generated_at_utc": utc_now_iso(), "industries": [], "collector": "kosis-industry-cycle-v1", "reason": "KOSIS_API_KEY is not configured"}
        write_json(output, result)
        return result
    config = read_json(root / "config" / "industry_kosis_sources.json", {}) or {}
    universe, _, _ = load_all(root)
    overrides = config.get("industry_keyword_overrides") or {}
    metric_rows: dict[str, list[dict[str, Any]]] = {}
    diagnostics: list[str] = []
    budget = _CallBudget(max(1, int(config.get("max_external_calls", 6))), errors=[], events=[])
    for name, spec in (config.get("series") or {}).items():
        if budget.attempts >= budget.limit:
            diagnostics.append("KOSIS call cap reached before remaining series")
            break
        budget.start_scope(int(spec.get("max_external_calls", 1)))
        try:
            org_id, table_id, rows = _choose_table(api_key, spec, budget)
            source = f"KOSIS org={org_id} table={table_id} factor={name}"
            metric_rows[name] = []
            for industry in universe.get("industries") or []:
                key = str(industry.get("key") or "")
                keywords = list(overrides.get(key) or [])
                if not keywords:
                    keywords = [str(industry.get("label") or "")]
                grouped = _series_by_label(rows, [word for word in keywords if word])
                merged = [item for values in grouped.values() for item in values]
                metric = _metric(merged, str(spec.get("factor") or name), source)
                if metric:
                    metric["industry_key"] = key
                    metric_rows[name].append(metric)
            diagnostics.append(f"{name}: table={table_id} rows={len(rows)} matched={len(metric_rows[name])}")
        except Exception as exc:
            diagnostics.append(f"{name}: {type(exc).__name__}: {exc}")
            if budget.attempts >= budget.limit:
                diagnostics.append("KOSIS call cap reached")
                break
    by_key: dict[str, dict[str, Any]] = {}
    for name, metrics in metric_rows.items():
        for metric in metrics:
            key = metric.pop("industry_key")
            by_key.setdefault(key, {"industry_key": key, "current": {"metrics": []}, "forecasts": {"3m": {"metrics": []}, "3_6m": {"metrics": []}, "6_12m": {"metrics": []}}})
            by_key[key]["current"]["metrics"].append(metric)
    result = {
        "schema_version": "1.0.0", "status": "raw" if by_key else "pending",
        "generated_at_utc": utc_now_iso(), "industries": list(by_key.values()),
        "collector": "kosis-industry-cycle-v2", "external_calls": budget.attempts,
        "diagnostics": diagnostics + (budget.events or []) + (budget.errors or []), "missing_data_policy": "do_not_impute_or_neutral_fill",
    }
    write_json(output, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect official KOSIS industry observations into the raw cycle contract")
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    print(f"KOSIS_KEY_CONFIGURED={'true' if os.getenv('KOSIS_API_KEY', '').strip() else 'false'}", flush=True)
    result = collect(Path(args.root).resolve())
    print(json.dumps({"status": result.get("status"), "industry_count": len(result.get("industries") or []), "external_calls": result.get("external_calls", 0)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
