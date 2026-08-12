"""
KOSIS industry-cycle collector v4.
Focus: low external-call volume, cache-first behavior, metadata-aware selectors,
and explicit missing-data handling. No imputation.
"""
from __future__ import annotations
import json, time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

CACHE_TTL_SECONDS = 24 * 3600
DEFAULT_CACHE_DIR = Path("input_cache") / "kosis"

def _meta_period(meta: Optional[dict]) -> Optional[str]:
    """Return the usable period field from KOSIS metadata without assuming a shape."""
    if not meta:
        return None
    for key in ("PRD_DE", "period", "PERIOD", "prdDe", "PRD_SE"):
        value = meta.get(key)
        if value not in (None, ""):
            return str(value)
    return None

def _fresh(path: Path, ttl: int = CACHE_TTL_SECONDS) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < ttl

def cache_json(path: Path, loader: Callable[[], Any], ttl: int = CACHE_TTL_SECONDS) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _fresh(path, ttl):
        return json.loads(path.read_text(encoding="utf-8"))
    value = loader()
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return value

def _pick_code(items: Iterable[dict], names: Iterable[str]) -> Optional[str]:
    targets = [str(x).strip().lower() for x in names]
    for row in items:
        name = str(row.get("NM") or row.get("OBJ_NM") or row.get("name") or "").strip().lower()
        code = row.get("CODE") or row.get("OBJ_ID") or row.get("code")
        if code is not None and any(t in name for t in targets):
            return str(code)
    return None

def build_selectors(meta: dict, preferred_names: Iterable[str]) -> List[Dict[str, str]]:
    """Build conservative selector candidates from metadata; never invent a code."""
    rows = meta.get("rows") if isinstance(meta, dict) else None
    if not isinstance(rows, list):
        rows = meta.get("data") if isinstance(meta, dict) else []
    rows = rows if isinstance(rows, list) else []
    code = _pick_code(rows, preferred_names)
    candidates: List[Dict[str, str]] = []
    if code:
        candidates.append({"objL1": code})
    candidates.append({"objL1": "ALL"})
    return candidates

def choose_period(meta: Optional[dict]) -> Optional[str]:
    return _meta_period(meta)

def select_metric(rows: List[dict], preferred_units: Iterable[str] = ()) -> Optional[dict]:
    """Select one actual row; do not aggregate unrelated series."""
    if not rows:
        return None
    units = [x.lower() for x in preferred_units]
    for row in rows:
        unit = str(row.get("UNIT_NM") or row.get("unit") or "").lower()
        if any(u in unit for u in units):
            return row
    return rows[0]

def normalize_metric(row: dict, factor: str, source: str) -> dict:
    return {
        "id": factor,
        "factor": factor,
        "value": row.get("DT") if row.get("DT") not in (None, "") else row.get("value"),
        "unit": row.get("UNIT_NM") or row.get("unit") or "unknown",
        "change_1m": row.get("change_1m"),
        "change_3m": row.get("change_3m"),
        "change_6m": row.get("change_6m"),
        "long_run_percentile": row.get("long_run_percentile"),
        "quality": row.get("quality"),
        "source": source,
        "series_id": row.get("series_id") or factor,
        "as_of": row.get("PRD_DE") or row.get("as_of"),
    }

def collect_with_cache(key: str, fetcher: Callable[[], Any], cache_dir: Path = DEFAULT_CACHE_DIR) -> Any:
    return cache_json(cache_dir / f"{key}.json", fetcher)
