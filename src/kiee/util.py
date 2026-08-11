from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def finite(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def roundn(value: Any, digits: int = 2) -> float | None:
    number = finite(value)
    return round(number, digits) if number is not None else None


def read_json(path: str | Path, default: Any = None) -> Any:
    file_path = Path(path)
    if not file_path.exists():
        return default
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: str | Path, payload: Any) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp = file_path.with_suffix(file_path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    temp.replace(file_path)


def nested(obj: Any, *keys: Any, default: Any = None) -> Any:
    current = obj
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def find_month_row(rows: Any, months: int) -> dict[str, Any]:
    if not isinstance(rows, list):
        return {}
    for row in rows:
        if isinstance(row, dict) and int(finite(row.get("months"), -999) or -999) == months:
            return row
    return {}


def weighted_average(items: Iterable[tuple[float | None, float]], default: float = 50.0) -> tuple[float, float]:
    num = 0.0
    den = 0.0
    for value, weight in items:
        if value is None or weight <= 0:
            continue
        num += float(value) * float(weight)
        den += float(weight)
    return ((num / den) if den else default, den)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def age_hours(value: Any, now: datetime | None = None) -> float | None:
    dt = parse_iso(value)
    if not dt:
        return None
    ref = now or datetime.now(timezone.utc)
    return max(0.0, (ref - dt).total_seconds() / 3600.0)
