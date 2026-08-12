from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
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
DATA_FALLBACK_URL = "https://sso.kosis.kr/openapi/Param/statisticsParameterData.do"

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
    clean = {key: value for key, value in params.items() if value not in (None, "")}
    query = urllib.parse.urlencode(clean)
    cache_root = os.getenv("KIEE_KOSIS_CACHE_DIR", "").strip()
    ttl_hours = float(os.getenv("KIEE_KOSIS_CACHE_TTL_HOURS", "24"))
    cache_path = None
    if cache_root:
        digest = hashlib.sha256((url + "?" + query).encode("utf-8")).hexdigest()
        cache_path = Path(cache_root) / f"{digest}.json"
        try:
            if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) <= ttl_hours * 3600:
                return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    request = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": "kiee-industry-cycle/1.0"})
    budget.allow()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if cache_path is not None:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        return payload
    except Exception as exc:
        if budget.errors is not None:
            table = params.get("tblId") or params.get("searchNm") or ""
            budget.errors.append(f"{url.rsplit('/', 1)[-1]}[{table}]: {type(exc).__name__}: {exc}")
        raise


def _get_data_json(params: dict[str, Any], budget: _CallBudget, allow_fallback: bool = True) -> Any:
    """Use the main KOSIS data host, then one bounded SSO-host fallback."""
    try:
        return _get_json(DATA_URL, params, budget, timeout=20)
    except (TimeoutError, urllib.error.URLError) as first_error:
        if not allow_fallback:
            raise
        if budget.events is not None:
            budget.events.append(f"data_host_fallback: {type(first_error).__name__}")
        return _get_json(DATA_FALLBACK_URL, params, budget, timeout=20)


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


def _meta_items(api_key: str, org_id: str, table_id: str, budget: _CallBudget) -> list[dict[str, str]]:
    rows = _rows(_get_json(META_URL, {
        "method": "getMeta", "type": "ITM", "apiKey": api_key,
        "orgId": org_id, "tblId": table_id, "format": "json", "jsonVD": "Y",
    }, budget))
    out: list[dict[str, str]] = []
    for row in rows:
        obj_id = str(row.get("OBJ_ID") or row.get("objId") or "").strip()
        obj_nm = str(row.get("OBJ_NM") or row.get("objNm") or "").strip()
        nm = str(row.get("NM") or row.get("nm") or "").strip()
        itm_id = str(row.get("ITM_ID") or row.get("itmId") or "").strip()
        itm_nm = str(row.get("ITM_NM") or row.get("itmNm") or "").strip()
        if obj_id or itm_id:
            out.append({"obj_id": obj_id, "obj_nm": obj_nm, "nm": nm, "itm_id": itm_id, "itm_nm": itm_nm})
    return out


def _select_query_codes(meta: list[dict[str, str]], industry_keywords: list[str], item_keywords: list[str]) -> tuple[dict[str, str], list[str]]:
    """Build selectors from KOSIS metadata.

    KOSIS ITM metadata semantics are: OBJ_ID=classification-value code,
    OBJ_NM=classification name, NM=classification-value name. The previous
    patch incorrectly treated OBJ_ID as C1/C2 level and NM as the code. That
    can generate invalid objL requests. We instead derive classifier levels
    from the ordered unique OBJ_NM values and use OBJ_ID as the actual code.
    """
    classifiers: dict[str, list[tuple[str, str]]] = {}
    order: list[str] = []
    item_matches: list[str] = []
    item_seen: set[str] = set()
    for row in meta:
        classifier = row["obj_nm"] or "__UNKNOWN__"
        code = row["obj_id"]
        value_name = row["nm"]
        if code:
            if classifier not in classifiers:
                classifiers[classifier] = []
                order.append(classifier)
            classifiers[classifier].append((code, value_name))
        iid = row["itm_id"]
        text = row["itm_nm"]
        if iid and iid not in item_seen and (not item_keywords or any(k in text for k in item_keywords)):
            item_matches.append(iid)
            item_seen.add(iid)

    selectors: dict[str, str] = {}
    industry_level = None
    industry_codes: list[str] = []
    for idx, classifier in enumerate(order, start=1):
        matches = [code for code, name in classifiers[classifier] if any(k in name for k in industry_keywords)]
        if matches:
            # Prefer the classifier with the largest useful industry match set;
            # this is typically the 산업별 dimension rather than 시도/지역.
            if industry_level is None or len(matches) > len(industry_codes):
                industry_level = idx
                industry_codes = list(dict.fromkeys(matches))

    for idx in range(1, len(order) + 1):
        if industry_level == idx:
            selectors[f"objL{idx}"] = " ".join(industry_codes)
        else:
            selectors[f"objL{idx}"] = "ALL"
    return selectors, item_matches


def _fetch_table(api_key: str, org_id: str, table_id: str, periods: int, budget: _CallBudget, industry_keywords: list[str] | None = None, item_keywords: list[str] | None = None) -> list[dict[str, Any]]:
    """Fetch a table using real KOSIS metadata codes, not invented selectors.

    KOSIS requires objL1 and itmId for the parameter endpoint. The previous
    implementation sent ALL for both, which caused error 20/21 on tables where
    ALL is not a valid item or classifier value. Metadata is fetched once and
    then used to construct one bounded bulk query.
    """
    period = _meta_period(api_key, org_id, table_id, budget)
    meta = _meta_items(api_key, org_id, table_id, budget)
    if not meta:
        raise RuntimeError(f"{table_id}: no ITM metadata")
    selectors, item_ids = _select_query_codes(meta, industry_keywords or [], item_keywords or [])
    if "objL1" not in selectors:
        # Some tables have a non-numeric OBJ_ID. Fall back only if metadata
        # contains an explicit first-level value; never fabricate a code.
        first = next((r["nm"] for r in meta if r["obj_id"] and r["nm"]), "")
        if first:
            selectors["objL1"] = first
    if not item_ids:
        item_ids = list(dict.fromkeys(r["itm_id"] for r in meta if r["itm_id"]))
    if not selectors.get("objL1") or not item_ids:
        raise RuntimeError(f"{table_id}: metadata did not expose usable objL1/itmId")

    params = {
        "method": "getList", "apiKey": api_key, "orgId": org_id, "tblId": table_id,
        "prdSe": period, "newEstPrdCnt": min(max(int(periods), 7), 24),
        "format": "json", "jsonVD": "Y", "itmId": " ".join(item_ids),
    }
    params.update(selectors)
    try:
        rows = _rows(_get_data_json(params, budget, allow_fallback=True))
        if rows:
            if budget.events is not None:
                budget.events.append(f"metadata_query: {table_id} objL1={selectors.get('objL1','')} itm={len(item_ids)} rows={len(rows)}")
            return rows
    except RuntimeError as exc:
        # One bounded retry with only the first matched industry code and first
        # matched item reduces the response size without turning into a call loop.
        text = str(exc)
        if "KOSIS error 31" in text or "KOSIS error 21" in text:
            first_obj = selectors.get("objL1", "").split()[0]
            first_itm = item_ids[0]
            retry = dict(params)
            retry["objL1"] = first_obj
            retry["itmId"] = first_itm
            rows = _rows(_get_data_json(retry, budget, allow_fallback=False))
            if rows:
                return rows
        raise
    return []

def _choose_table(api_key: str, spec: dict[str, Any], budget: _CallBudget) -> tuple[str, str, list[dict[str, Any]]]:
    periods = min(int(spec.get("periods", 24)), 24)
    preferred = {
        str(table).strip(): index
        for index, table in enumerate(spec.get("preferred_tables") or [])
        if str(table).strip()
    }

    # Search is useful for discovery but is a separate, less reliable request.
    # When a reviewed table id is already configured, query it directly first;
    # this avoids turning a search timeout into a false "no data" result and
    # also saves one external request for the normal path.
    direct_org = str(spec.get("org_id") or "101").strip()
    direct_errors: list[str] = []
    direct_limit = max(1, int(spec.get("direct_probe_candidates", 1)))
    for table_id in list(preferred)[:direct_limit]:
        try:
            rows = _fetch_table(api_key, direct_org, table_id, periods, budget, list(spec.get("keywords") or []), list(spec.get("item_keywords") or []))
            if rows:
                if budget.events is not None:
                    budget.events.append(f"direct_table: {direct_org}/{table_id} rows={len(rows)}")
                return direct_org, table_id, rows
        except Exception as exc:
            direct_errors.append(f"{table_id}: {type(exc).__name__}: {exc}")

    # A configured table is authoritative for this factor. If its parameter
    # query fails, do not spend more calls on a broad search and unrelated
    # candidate tables in the same run.
    if preferred and direct_errors:
        if budget.events is not None:
            budget.events.append("direct_table_only: preferred table query failed; search skipped")
        raise RuntimeError(direct_errors[-1])

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
            rows = _fetch_table(api_key, org, table, periods, budget, list(spec.get("keywords") or []), list(spec.get("item_keywords") or []))
            if rows:
                return org, table, rows
        except Exception as exc:
            probe_errors.append(f"{table}: {type(exc).__name__}: {exc}")
            continue
    if budget.events is not None and candidates:
        budget.events.append(f"table_probe: no usable rows for {','.join(probed)}")
        budget.events.extend(f"table_probe_error: {error}" for error in probe_errors)
        budget.events.extend(f"direct_table_error: {error}" for error in direct_errors)
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
        key = "|".join(str(row.get(f"C{i}") or "") for i in range(1, 5))
        grouped.setdefault(key, []).append((period, value))
    return grouped


def _best_series(grouped: dict[str, list[tuple[str, float]]]) -> list[tuple[str, float]]:
    if not grouped:
        return []
    # Do not merge regions / classifications. Prefer the longest series and,
    # where available, the national aggregate (code/name containing 전국).
    ranked = sorted(grouped.items(), key=lambda kv: (len(kv[1]), 1 if "전국" in kv[0] else 0), reverse=True)
    return ranked[0][1]

def _metric(series: list[tuple[str, float]], factor: str, source: str, unit: str = "KOSIS") -> dict[str, Any] | None:
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
        "id": factor, "factor": factor, "value": latest, "unit": unit,
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
    os.environ.setdefault("KIEE_KOSIS_CACHE_DIR", str(root / "input_cache" / "kosis_api"))
    os.environ.setdefault("KIEE_KOSIS_CACHE_TTL_HOURS", str(config.get("cache_ttl_hours", 24)))
    universe, _, _ = load_all(root)
    overrides = config.get("industry_keyword_overrides") or {}
    previous = read_json(output, {}) or {}
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
                series = _best_series(grouped)
                metric = _metric(series, str(spec.get("factor") or name), source)
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
        "collector": "kosis-industry-cycle-v3", "external_calls": budget.attempts,
        "diagnostics": diagnostics + (budget.events or []) + (budget.errors or []), "missing_data_policy": "do_not_impute_or_neutral_fill",
    }
    # Preserve the last valid raw observations when a transient KOSIS outage
    # returns no rows. The failed attempt remains visible in diagnostics, but
    # a timeout must not erase the last-known-good input used by the batch feed.
    if not by_key and isinstance(previous, dict) and previous.get("industries") and previous.get("status") in {"raw", "scored"}:
        result = dict(previous)
        result["generated_at_utc"] = utc_now_iso()
        result["last_attempt_status"] = "pending"
        result["last_attempt_external_calls"] = budget.attempts
        result["last_attempt_diagnostics"] = diagnostics + (budget.events or []) + (budget.errors or [])
        result["missing_data_policy"] = "do_not_impute_or_neutral_fill"
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
