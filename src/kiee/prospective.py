from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from .util import clamp, finite, parse_iso, read_json, roundn, utc_now_iso, write_json


def _date_key(value: str) -> str:
    return str(value or "")[:10]


def _month_key(value: str) -> str:
    return str(value or "")[:7]


def _days_between(a: str, b: str) -> float | None:
    da = parse_iso(a if "T" in a else a + "T00:00:00+00:00")
    db = parse_iso(b if "T" in b else b + "T00:00:00+00:00")
    if not da or not db:
        return None
    return (db - da).total_seconds() / 86400.0


def _actual_equal_weight_return(entry: dict[str, Any], current_closes: dict[str, Any]) -> tuple[float | None, int]:
    old = entry.get("member_closes") or {}
    returns: list[float] = []
    for code, old_close in old.items():
        a = finite(old_close)
        b = finite(current_closes.get(code))
        if a is None or b is None or a <= 0:
            continue
        returns.append((b / a - 1.0) * 100.0)
    return (mean(returns), len(returns)) if returns else (None, 0)


def update_registry(root: Path, results: list[dict[str, Any]], direct_market: dict[str, Any], policy: dict[str, Any], as_of: str) -> dict[str, Any]:
    validation_dir = root / "output" / "validation"
    registry_path = validation_dir / "forecast_registry.json"
    report_path = validation_dir / "prospective_validation.json"
    registry = read_json(registry_path, {}) or {}
    entries = registry.get("entries") if isinstance(registry, dict) else None
    if not isinstance(entries, list):
        entries = []

    # One immutable forecast per industry/month prevents daily overlapping forecasts from overstating sample size.
    existing_keys = {(str(e.get("industry_key")), _month_key(str(e.get("as_of")))) for e in entries if isinstance(e, dict)}
    direct_rows = direct_market.get("industries") or {}
    for row in results:
        key = str(row.get("industry_key"))
        pair = (key, _month_key(as_of))
        if pair in existing_keys:
            continue
        direct = direct_rows.get(key) or {}
        closes = direct.get("member_closes") or {}
        if not closes:
            continue
        entries.append({
            "industry_key": key,
            "industry_label": row.get("industry_label"),
            "as_of": as_of,
            "current_score": row.get("current", {}).get("score"),
            "forecast_3m_score": row.get("forecast_3m", {}).get("score"),
            "forecast_3m_v2_shadow_score": row.get("forecast_3m", {}).get("score_v2_shadow"),
            "forecast_3_6m_score": row.get("forecast_3_6m", {}).get("score"),
            "forecast_3_6m_v2_shadow_score": row.get("forecast_3_6m", {}).get("score_v2_shadow"),
            "forecast_6_12m_score": row.get("forecast_6_12m", {}).get("score"),
            "forecast_6_12m_v2_shadow_score": row.get("forecast_6_12m", {}).get("score_v2_shadow"),
            "signal_normalized": row.get("stock_prediction_bridge", {}).get("signal_normalized"),
            "quality_score": row.get("forecast_3m", {}).get("quality_score"),
            "member_closes": closes,
            "evaluated": False,
        })
        existing_keys.add(pair)

    evaluated_rows: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("evaluated") is True:
            evaluated_rows.append(entry)
            continue
        age = _days_between(str(entry.get("as_of")), as_of)
        if age is None or age < 75:
            continue
        current = (direct_rows.get(str(entry.get("industry_key"))) or {}).get("member_closes") or {}
        actual_return, overlap = _actual_equal_weight_return(entry, current)
        if actual_return is None or overlap < 2:
            continue
        forecast = finite(entry.get("forecast_3m_score"), 50.0) or 50.0
        predicted_sign = 1 if forecast >= 53 else (-1 if forecast <= 47 else 0)
        actual_sign = 1 if actual_return >= 2 else (-1 if actual_return <= -2 else 0)
        entry["evaluated"] = True
        entry["evaluated_at"] = as_of
        entry["actual_3m_equal_weight_return_pct"] = roundn(actual_return, 3)
        entry["overlap_members"] = overlap
        entry["predicted_sign"] = predicted_sign
        entry["actual_sign"] = actual_sign
        entry["direction_hit"] = (predicted_sign == actual_sign) if predicted_sign != 0 and actual_sign != 0 else None
        challenger = finite(entry.get("forecast_3m_v2_shadow_score"))
        if challenger is not None:
            challenger_sign = 1 if challenger >= 53 else (-1 if challenger <= 47 else 0)
            entry["challenger_v2_predicted_sign"] = challenger_sign
            entry["challenger_v2_direction_hit"] = (challenger_sign == actual_sign) if challenger_sign != 0 and actual_sign != 0 else None
            entry["challenger_v2_abs_error_to_return_direction"] = roundn(abs((challenger - 50.0) - clamp(actual_return, -50.0, 50.0)), 3)
        evaluated_rows.append(entry)

    direction_rows = [e for e in evaluated_rows if e.get("direction_hit") is not None]
    direction_accuracy = mean(1.0 if e.get("direction_hit") else 0.0 for e in direction_rows) if direction_rows else None
    positive_returns = [float(e["actual_3m_equal_weight_return_pct"]) for e in evaluated_rows if int(e.get("predicted_sign") or 0) > 0]
    negative_returns = [float(e["actual_3m_equal_weight_return_pct"]) for e in evaluated_rows if int(e.get("predicted_sign") or 0) < 0]
    spread = (mean(positive_returns) - mean(negative_returns)) if positive_returns and negative_returns else None

    challenger_direction_rows = [e for e in evaluated_rows if e.get("challenger_v2_direction_hit") is not None]
    challenger_accuracy = mean(1.0 if e.get("challenger_v2_direction_hit") else 0.0 for e in challenger_direction_rows) if challenger_direction_rows else None
    challenger_positive_returns = [float(e["actual_3m_equal_weight_return_pct"]) for e in evaluated_rows if int(e.get("challenger_v2_predicted_sign") or 0) > 0]
    challenger_negative_returns = [float(e["actual_3m_equal_weight_return_pct"]) for e in evaluated_rows if int(e.get("challenger_v2_predicted_sign") or 0) < 0]
    challenger_spread = (mean(challenger_positive_returns) - mean(challenger_negative_returns)) if challenger_positive_returns and challenger_negative_returns else None
    min_cases = int(policy.get("prospective_min_cases", 24))
    accuracy_min = float(policy.get("prospective_direction_accuracy_min", 0.55))
    spread_min = float(policy.get("prospective_mean_return_spread_min_pct", 2.0))
    passed = (
        len(evaluated_rows) >= min_cases
        and len(direction_rows) >= max(12, min_cases // 2)
        and direction_accuracy is not None and direction_accuracy >= accuracy_min
        and spread is not None and spread >= spread_min
    )
    status = "PASSED" if passed else ("EVALUATING" if evaluated_rows else "PENDING")
    if passed:
        quality = min(95.0, 80.0 + (direction_accuracy - accuracy_min) * 100.0 + min(10.0, max(0.0, (spread or 0) - spread_min)))
    elif evaluated_rows:
        quality = min(65.0, 35.0 + len(evaluated_rows) * 1.2 + (direction_accuracy or 0.0) * 20.0)
    else:
        quality = 30.0
    challenger_has_sample = len(challenger_direction_rows) >= max(12, min_cases // 2)
    challenger_superior = (
        len(evaluated_rows) >= min_cases
        and challenger_has_sample
        and challenger_accuracy is not None
        and direction_accuracy is not None
        and challenger_accuracy >= direction_accuracy + 0.02
        and challenger_spread is not None
        and (spread is None or challenger_spread >= spread)
    )
    summary = {
        "schema_version": "1.1.0",
        "status": status,
        "quality_score": round(quality, 1),
        "registered_forecasts": len(entries),
        "evaluated_cases": len(evaluated_rows),
        "directional_cases": len(direction_rows),
        "direction_accuracy": roundn(direction_accuracy, 4),
        "positive_signal_mean_return_pct": roundn(mean(positive_returns), 3) if positive_returns else None,
        "negative_signal_mean_return_pct": roundn(mean(negative_returns), 3) if negative_returns else None,
        "return_spread_pct_point": roundn(spread, 3),
        "challenger_v2": {
            "status": "PROMOTION_CANDIDATE" if challenger_superior else ("EVALUATING" if challenger_direction_rows else "PENDING"),
            "evaluated_cases": len([e for e in evaluated_rows if finite(e.get("forecast_3m_v2_shadow_score")) is not None]),
            "directional_cases": len(challenger_direction_rows),
            "direction_accuracy": roundn(challenger_accuracy, 4),
            "positive_signal_mean_return_pct": roundn(mean(challenger_positive_returns), 3) if challenger_positive_returns else None,
            "negative_signal_mean_return_pct": roundn(mean(challenger_negative_returns), 3) if challenger_negative_returns else None,
            "return_spread_pct_point": roundn(challenger_spread, 3),
            "legacy_direction_accuracy": roundn(direction_accuracy, 4),
            "legacy_return_spread_pct_point": roundn(spread, 3),
            "promotion_rule": "minimum 24 evaluated cases; challenger direction accuracy >= legacy +2%p and return spread >= legacy",
            "production_score_unchanged": True,
        },
        "requirements": {
            "min_evaluated_cases": min_cases,
            "direction_accuracy_min": accuracy_min,
            "return_spread_min_pct_point": spread_min,
            "evaluation_horizon_calendar_days_min": 75,
            "same_forecast_month_deduplicated": True,
        },
        "note": "산업전망 자체의 3개월 주가방향 OOS가 축적되기 전에는 개별종목 예측에서 보조 오버레이의 최대 영향도를 제한합니다.",
        "generated_at_utc": utc_now_iso(),
    }
    write_json(registry_path, {"schema_version": "1.1.0", "entries": entries[-1200:]})
    write_json(report_path, summary)
    return summary


def read_summary(root: Path) -> dict[str, Any]:
    return read_json(root / "output" / "validation" / "prospective_validation.json", {}) or {
        "status": "PENDING", "quality_score": 30.0, "evaluated_cases": 0,
    }
