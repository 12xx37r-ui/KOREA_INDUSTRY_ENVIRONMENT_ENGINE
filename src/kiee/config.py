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


def load_all(root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    industries = load_json_config(root / "config" / "industries.json")
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
