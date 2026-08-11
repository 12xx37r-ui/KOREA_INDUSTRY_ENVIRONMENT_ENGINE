from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ConfigError(RuntimeError):
    pass


def load_json_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"config missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be object: {path}")
    return data


def _profile_defaults(profile: str) -> dict[str, Any]:
    sensitivity = {
        "rate_relief": 0.35,
        "krw_weakness": 0.35,
        "liquidity": 0.35,
        "credit_health": 0.25,
        "consumer_cycle": 0.35,
        "cost_relief": 0.35,
        "global_equity": 0.35,
    }
    if profile == "manufacturing_export":
        sensitivity.update({"krw_weakness": 0.65, "consumer_cycle": 0.45, "cost_relief": 0.55, "global_equity": 0.75})
    elif profile == "capital_goods":
        sensitivity.update({"rate_relief": 0.65, "consumer_cycle": 0.30, "cost_relief": 0.45, "global_equity": 0.65})
    elif profile == "consumer_services":
        sensitivity.update({"rate_relief": 0.45, "consumer_cycle": 0.85, "cost_relief": 0.25, "global_equity": 0.30})
    elif profile == "consumer_manufacturing":
        sensitivity.update({"rate_relief": 0.35, "consumer_cycle": 0.75, "cost_relief": 0.40, "global_equity": 0.45})
    elif profile == "content_media" or profile == "digital_services":
        sensitivity.update({"rate_relief": 0.45, "consumer_cycle": 0.55, "global_equity": 0.55})
    elif profile == "energy" or profile == "materials":
        sensitivity.update({"krw_weakness": 0.55, "cost_relief": 0.70, "global_equity": 0.65})
    elif profile == "financial":
        sensitivity.update({"rate_relief": 0.20, "consumer_cycle": 0.45, "credit_health": 0.80, "global_equity": 0.55})
    elif profile == "real_estate":
        sensitivity.update({"rate_relief": 0.90, "consumer_cycle": 0.55, "credit_health": 0.75, "global_equity": 0.35})
    elif profile == "regulated_services":
        sensitivity.update({"rate_relief": 0.25, "consumer_cycle": 0.20, "global_equity": 0.20})
    return sensitivity


def _default_industry_row(profile: dict[str, Any]) -> dict[str, Any]:
    kind = str(profile.get("profile") or "general")
    return {
        "key": str(profile["key"]),
        "label": str(profile["label"]),
        "aliases": list(profile.get("aliases") or []),
        "theme_ids": [],
        "krx_basket": [],
        "sensitivities": _profile_defaults(kind),
        "weights_current": {
            "earnings_momentum": 0.20, "demand_cycle": 0.20, "pricing_margin": 0.15,
            "financial_conditions": 0.10, "market_internals": 0.15, "valuation": 0.20,
        },
        "weights_3m": {
            "earnings_momentum": 0.15, "demand_cycle": 0.25, "pricing_margin": 0.15,
            "financial_conditions": 0.15, "market_internals": 0.15, "valuation": 0.15,
        },
        "theme_relevance": 0.0,
        "notes": "산업실물지표 피드 연결 전에는 점수를 산출하지 않음",
    }


def _merge_industry_universe(root: Path, industries: dict[str, Any]) -> dict[str, Any]:
    catalog_path = root / "config" / "industry_universe.json"
    if not catalog_path.exists():
        return industries
    catalog = load_json_config(catalog_path)
    profiles = catalog.get("industries") or []
    if not isinstance(profiles, list):
        raise ConfigError("industry_universe.json industries must be list")
    rows = list(industries.get("industries") or [])
    by_key = {str(row.get("key")): row for row in rows}
    common_current = list(catalog.get("common_current_metrics") or [])
    common_leading = list(catalog.get("common_leading_metrics") or [])
    for profile in profiles:
        key = str(profile.get("key") or "").strip()
        if not key:
            raise ConfigError("industry_universe.json contains empty key")
        row = by_key.get(key)
        if row is None:
            row = _default_industry_row(profile)
            rows.append(row)
            by_key[key] = row
        row["parent_sector"] = str(profile.get("parent_sector") or "미분류")
        row["industry_group"] = str(profile.get("industry_group") or "미분류")
        row["classification_basis"] = ["GICS", "WICS", "KSIC"]
        row["industry_profile"] = str(profile.get("profile") or "general")
        row["specialized_current_metrics"] = list(profile.get("specialized_current") or [])
        row["specialized_leading_metrics"] = list(profile.get("specialized_leading") or [])
        row["current_metric_groups"] = common_current + row["specialized_current_metrics"]
        row["leading_metric_groups"] = common_leading + row["specialized_leading_metrics"]
        row["data_status"] = "awaiting_industry_cycle_feed"
        row.setdefault("aliases", [])
        row["aliases"] = list(dict.fromkeys(list(row.get("aliases") or []) + list(profile.get("aliases") or [])))
    industries["schema_version"] = "2.0.0"
    industries["classification_basis"] = catalog.get("classification_basis") or ["GICS", "WICS", "KSIC"]
    industries["classification_rule"] = catalog.get("classification_rule") or "공통지표와 산업특화지표를 결합"
    industries["industries"] = rows
    return industries


def load_all(root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    industries = load_json_config(root / "config" / "industries.json")
    industries = _merge_industry_universe(root, industries)
    policy = load_json_config(root / "config" / "scoring_policy.json")
    upstreams = load_json_config(root / "config" / "upstreams.json")
    rows = industries.get("industries")
    if not isinstance(rows, list) or len(rows) < 20:
        raise ConfigError("industries.json requires at least 20 industries")
    seen: set[str] = set()
    for row in rows:
        key = str(row.get("key") or "").strip()
        if not key or key in seen:
            raise ConfigError(f"duplicate/empty industry key: {key!r}")
        seen.add(key)
        for weight_key in ("weights_current", "weights_3m"):
            weights = row.get(weight_key) or {}
            total = sum(float(v) for v in weights.values())
            if abs(total - 1.0) > 1e-8:
                raise ConfigError(f"{key} {weight_key} must sum to 1.0, got {total}")
    return industries, policy, upstreams
