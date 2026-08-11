from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .config import load_all
from .util import finite, read_json, utc_now_iso, write_json


def _metric_score(metric: dict[str, Any]) -> float | None:
    """Use only an explicitly normalized observation; never invent a neutral value."""
    score = None
    for candidate in (metric.get("score"), metric.get("normalized_score"), metric.get("long_run_percentile")):
        score = finite(candidate)
        if score is not None:
            break
    return max(0.0, min(100.0, float(score))) if score is not None else None


def _quality(metric: dict[str, Any]) -> float:
    value = finite(metric.get("quality"), metric.get("quality_score"))
    return max(0.0, min(100.0, float(value))) if value is not None else 0.0


def _normalize_metric(metric: dict[str, Any]) -> dict[str, Any]:
    result = dict(metric)
    score = _metric_score(metric)
    result["score"] = round(score, 4) if score is not None else None
    result["quality"] = round(_quality(metric), 2)
    result["available"] = score is not None and _quality(metric) > 0 and bool(metric.get("source")) and bool(metric.get("as_of"))
    return result


def _factor_scores(metrics: list[dict[str, Any]], weights: dict[str, Any]) -> tuple[dict[str, float], float | None, float, float]:
    grouped: dict[str, list[tuple[float, float]]] = {}
    for metric in metrics:
        if not isinstance(metric, dict) or metric.get("available") is not True:
            continue
        factor = str(metric.get("factor") or "").strip()
        score = finite(metric.get("score"))
        quality = _quality(metric)
        if not factor or score is None or quality <= 0:
            continue
        grouped.setdefault(factor, []).append((float(score), quality))
    scores: dict[str, float] = {}
    weighted_total = 0.0
    weight_total = 0.0
    quality_total = 0.0
    for factor, raw_weight in (weights or {}).items():
        weight = float(raw_weight or 0.0)
        values = grouped.get(str(factor), [])
        if not values or weight <= 0:
            continue
        q_weight = sum(q for _, q in values)
        score = sum(value * q for value, q in values) / q_weight
        quality = q_weight / len(values)
        scores[str(factor)] = round(score, 4)
        weighted_total += score * weight
        weight_total += weight
        quality_total += quality * weight
    total_weights = max(sum(float(v or 0.0) for v in (weights or {}).values()), 1e-9)
    coverage = 100.0 * weight_total / total_weights
    quality_score = quality_total / max(weight_total, 1e-9) if weight_total else 0.0
    aggregate = weighted_total / weight_total if weight_total else None
    return scores, (round(aggregate, 4) if aggregate is not None else None), round(quality_score, 2), round(coverage, 2)


def _stage(raw_stage: Any, weights: dict[str, Any], minimum_coverage: float, horizon: str) -> dict[str, Any]:
    source = raw_stage if isinstance(raw_stage, dict) else {}
    metrics = [_normalize_metric(item) for item in (source.get("metrics") or []) if isinstance(item, dict)]
    factor_scores, calculated_score, calculated_quality, calculated_coverage = _factor_scores(metrics, weights)
    coverage = calculated_coverage
    # A collector may provide a reproducible aggregate after using a more specific
    # industry model. It is accepted only with factor scores and enough coverage.
    declared_score = finite(source.get("score"))
    score = declared_score if declared_score is not None else calculated_score
    declared_coverage = finite(source.get("data_coverage_pct"))
    coverage = declared_coverage if declared_coverage is not None else coverage
    quality = finite(source.get("quality_score"), calculated_quality) or 0.0
    passed = score is not None and quality > 0 and coverage >= minimum_coverage
    result = dict(source)
    result["metrics"] = metrics
    result["factor_scores"] = factor_scores
    result["data_coverage_pct"] = round(max(0.0, min(100.0, coverage)), 2)
    result["quality_score"] = round(max(0.0, min(100.0, quality)), 2)
    result["score"] = round(max(0.0, min(100.0, float(score))), 2) if passed else None
    result["status"] = "scored" if passed else "insufficient_data"
    result["reason"] = "" if passed else f"{horizon}: normalized industry metrics are below the {minimum_coverage:.0f}% coverage gate"
    return result


def build_feed(root: Path, raw_path: Path | None = None, output_path: Path | None = None) -> dict[str, Any]:
    _, policy, _ = load_all(root)
    raw_path = raw_path or root / "input" / "industry_cycle_raw.json"
    output_path = output_path or root / "input" / "industry_cycle_latest.json"
    raw = read_json(raw_path, {}) or {}
    cycle_policy = policy.get("industry_cycle_scoring") or {}
    current_weights = cycle_policy.get("current_weights") or {}
    leading_weights = cycle_policy.get("leading_weights") or {}
    current_min = float(cycle_policy.get("minimum_current_metric_coverage_pct", 60.0))
    leading_min = float(cycle_policy.get("minimum_leading_metric_coverage_pct", 60.0))
    if not isinstance(raw, dict) or raw.get("status") not in {"raw", "scored"}:
        result = {
            "schema_version": "1.0.0", "status": "pending", "generated_at_utc": utc_now_iso(),
            "industries": [], "collector": "local-industry-cycle-batch-v1",
            "reason": "input/industry_cycle_raw.json is absent or not marked raw/scored; no industry score was generated",
        }
        write_json(output_path, result)
        return result

    rows: list[dict[str, Any]] = []
    for item in raw.get("industries") or []:
        if not isinstance(item, dict) or not str(item.get("industry_key") or "").strip():
            continue
        current = _stage(item.get("current"), current_weights, current_min, "current")
        forecasts_raw = item.get("forecasts") if isinstance(item.get("forecasts"), dict) else {}
        forecasts = {
            "3m": _stage(forecasts_raw.get("3m"), leading_weights, leading_min, "3m"),
            "3_6m": _stage(forecasts_raw.get("3_6m"), leading_weights, leading_min, "3_6m"),
            "6_12m": _stage(forecasts_raw.get("6_12m"), leading_weights, leading_min, "6_12m"),
        }
        rows.append({
            "industry_key": str(item["industry_key"]), "current": current, "forecasts": forecasts,
            "specialized_metrics": item.get("specialized_metrics") or [],
            "source_provenance": item.get("source_provenance") or {},
        })
    scored_count = sum(1 for row in rows if row["current"].get("status") == "scored")
    result = {
        "schema_version": "1.0.0", "status": "scored" if scored_count else "pending",
        "generated_at_utc": str(raw.get("generated_at_utc") or utc_now_iso()),
        "collector": "local-industry-cycle-batch-v1", "industries": rows,
        "scored_industry_count": scored_count, "raw_input_generated_at_utc": raw.get("generated_at_utc"),
        "missing_data_policy": "do_not_impute_or_neutral_fill",
    }
    write_json(output_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and batch-build the authoritative industry-cycle feed")
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    result = build_feed(Path(args.root).resolve())
    print(json.dumps({"status": result.get("status"), "scored_industry_count": result.get("scored_industry_count", 0)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
