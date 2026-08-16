from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .util import read_json, utc_now_iso, write_json

# Run-scoped network guard.  The goal is freshness first: this module never skips a
# live request merely because a wall-clock TTL has not expired.  It only removes
# exact duplicate requests inside the same process and smooths provider bursts.
_LOCK = threading.Lock()
_MEMO: dict[tuple[str, str, str], bytes] = {}
_LAST_CALL_AT: dict[str, float] = defaultdict(float)
_STATS: dict[str, dict[str, Any]] = defaultdict(lambda: {
    "network_calls": 0,
    "duplicate_calls_removed": 0,
    "cache_uses": 0,
    "retries": 0,
    "rate_limit_429": 0,
    "timeouts": 0,
    "fallback_used": False,
    "lkg_used": False,
    "unavailable": 0,
    "state": "",
})

_DEFAULT_INTERVALS = {
    "KOSIS": 0.12,
    "CUSTOMS": 0.18,
    "DART": 0.12,
    "GITHUB": 0.08,
    "KRX": 0.15,
}


def _interval(provider: str) -> float:
    env = os.getenv(f"KIEE_{provider.upper()}_MIN_INTERVAL_SECONDS", "").strip()
    if env:
        try:
            return max(0.0, float(env))
        except ValueError:
            pass
    return _DEFAULT_INTERVALS.get(provider.upper(), 0.1)


def _sleep_for_provider(provider: str) -> None:
    provider = provider.upper()
    with _LOCK:
        wait = _interval(provider) - (time.monotonic() - _LAST_CALL_AT[provider])
    if wait > 0:
        time.sleep(wait)


def _mark_call(provider: str) -> None:
    provider = provider.upper()
    with _LOCK:
        _LAST_CALL_AT[provider] = time.monotonic()
        _STATS[provider]["network_calls"] += 1


def _retry_after_seconds(headers: Any) -> float | None:
    if not headers:
        return None
    value = headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            dt = parsedate_to_datetime(str(value))
            return max(0.0, dt.timestamp() - time.time())
        except Exception:
            return None


def request_bytes(
    provider: str,
    request: urllib.request.Request,
    *,
    timeout: float = 8.0,
    memo_key: str | None = None,
    max_retries: int = 2,
) -> bytes:
    """Freshness-first HTTP with exact in-run dedupe and bounded retry.

    memo_key must represent the exact logical source (same provider/symbol/series).
    Cross-run TTL caching is intentionally not performed here.
    """
    provider = provider.upper()
    method = str(getattr(request, "method", None) or request.get_method() or "GET").upper()
    key = (provider, method, memo_key or request.full_url)
    if method == "GET":
        with _LOCK:
            if key in _MEMO:
                _STATS[provider]["duplicate_calls_removed"] += 1
                return _MEMO[key]

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        if attempt:
            with _LOCK:
                _STATS[provider]["retries"] += 1
        _sleep_for_provider(provider)
        _mark_call(provider)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                if method == "GET":
                    with _LOCK:
                        _MEMO[key] = body
                return body
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code == 429:
                with _LOCK:
                    _STATS[provider]["rate_limit_429"] += 1
                if attempt >= max_retries:
                    raise
                retry_after = _retry_after_seconds(exc.headers)
                delay = retry_after if retry_after is not None else min(8.0, 0.6 * (2 ** attempt) + random.uniform(0.05, 0.35))
                time.sleep(delay)
                continue
            if 500 <= int(exc.code) <= 599 and attempt < max_retries:
                time.sleep(min(5.0, 0.4 * (2 ** attempt) + random.uniform(0.05, 0.30)))
                continue
            raise
        except (TimeoutError, urllib.error.URLError) as exc:
            last_exc = exc
            text = str(getattr(exc, "reason", exc)).lower()
            if isinstance(exc, TimeoutError) or "timed out" in text or "timeout" in text:
                with _LOCK:
                    _STATS[provider]["timeouts"] += 1
            if attempt >= max_retries:
                raise
            time.sleep(min(4.0, 0.35 * (2 ** attempt) + random.uniform(0.05, 0.25)))
    if last_exc:
        raise last_exc
    raise RuntimeError("network request failed without exception")


def record_state(provider: str, state: str) -> None:
    state = str(state or "UNAVAILABLE").upper()
    if state not in {"LIVE", "CACHE", "LKG", "FALLBACK", "UNAVAILABLE"}:
        state = "UNAVAILABLE"
    # More degraded states dominate within one workflow so failures are never
    # hidden by an earlier successful call.
    rank = {"LIVE": 0, "CACHE": 1, "FALLBACK": 2, "LKG": 3, "UNAVAILABLE": 4}
    with _LOCK:
        current = str(_STATS[provider.upper()].get("state") or "")
        if not current or rank.get(state, 4) >= rank.get(current, 4):
            _STATS[provider.upper()]["state"] = state


def record_cache(provider: str, count: int = 1) -> None:
    with _LOCK:
        _STATS[provider.upper()]["cache_uses"] += max(0, int(count))
    record_state(provider, "CACHE")


def record_fallback(provider: str, used: bool = True) -> None:
    if used:
        with _LOCK:
            _STATS[provider.upper()]["fallback_used"] = True
        record_state(provider, "FALLBACK")


def record_lkg(provider: str, used: bool = True) -> None:
    if used:
        with _LOCK:
            _STATS[provider.upper()]["lkg_used"] = True
        record_state(provider, "LKG")


def record_unavailable(provider: str, count: int = 1) -> None:
    with _LOCK:
        _STATS[provider.upper()]["unavailable"] += max(0, int(count))
    record_state(provider, "UNAVAILABLE")


def add_network_calls(provider: str, count: int) -> None:
    """For libraries (e.g. pykrx) whose HTTP layer is not urllib in this process."""
    if count > 0:
        with _LOCK:
            _STATS[provider.upper()]["network_calls"] += int(count)


def snapshot() -> dict[str, Any]:
    with _LOCK:
        providers = {k: dict(v) for k, v in sorted(_STATS.items())}
    totals: dict[str, Any] = {
        "network_calls": sum(int(v.get("network_calls", 0)) for v in providers.values()),
        "duplicate_calls_removed": sum(int(v.get("duplicate_calls_removed", 0)) for v in providers.values()),
        "cache_uses": sum(int(v.get("cache_uses", 0)) for v in providers.values()),
        "retries": sum(int(v.get("retries", 0)) for v in providers.values()),
        "rate_limit_429": sum(int(v.get("rate_limit_429", 0)) for v in providers.values()),
        "timeouts": sum(int(v.get("timeouts", 0)) for v in providers.values()),
        "fallback_used": any(bool(v.get("fallback_used")) for v in providers.values()),
        "lkg_used": any(bool(v.get("lkg_used")) for v in providers.values()),
    }
    return {
        "schema_version": "1.0.0",
        "generated_at_utc": utc_now_iso(),
        "policy": {
            "priority": "freshness_first",
            "live_market_rule": "when new market data may exist, source API is queried on every workflow",
            "dedupe_scope": "same process, identical provider+logical request",
            "cache_rule": "CACHE only for intentionally reused unchanged/closed-market/publication-cycle data",
            "retry_rule": "Retry-After first for 429; bounded exponential backoff+jitter for 429/5xx/timeouts",
            "states": ["LIVE", "CACHE", "LKG", "FALLBACK", "UNAVAILABLE"],
        },
        "providers": providers,
        "totals": totals,
    }


def flush(root: Path) -> dict[str, Any]:
    """Merge this process' counters into workflow-level output/api_health.json."""
    path = root / "output" / "api_health.json"
    current = snapshot()
    old = read_json(path, {}) or {}
    old_providers = old.get("providers") if isinstance(old, dict) else {}
    merged: dict[str, dict[str, Any]] = {}
    for provider in set(old_providers or {}) | set(current["providers"]):
        a = (old_providers or {}).get(provider) or {}
        b = current["providers"].get(provider) or {}
        merged[provider] = {
            "network_calls": int(a.get("network_calls", 0)) + int(b.get("network_calls", 0)),
            "duplicate_calls_removed": int(a.get("duplicate_calls_removed", 0)) + int(b.get("duplicate_calls_removed", 0)),
            "cache_uses": int(a.get("cache_uses", 0)) + int(b.get("cache_uses", 0)),
            "retries": int(a.get("retries", 0)) + int(b.get("retries", 0)),
            "rate_limit_429": int(a.get("rate_limit_429", 0)) + int(b.get("rate_limit_429", 0)),
            "timeouts": int(a.get("timeouts", 0)) + int(b.get("timeouts", 0)),
            "fallback_used": bool(a.get("fallback_used")) or bool(b.get("fallback_used")),
            "lkg_used": bool(a.get("lkg_used")) or bool(b.get("lkg_used")),
            "unavailable": int(a.get("unavailable", 0)) + int(b.get("unavailable", 0)),
            "state": b.get("state") or a.get("state") or "UNAVAILABLE",
        }
    out = dict(current)
    out["providers"] = merged
    out["totals"] = {
        "network_calls": sum(v["network_calls"] for v in merged.values()),
        "duplicate_calls_removed": sum(v["duplicate_calls_removed"] for v in merged.values()),
        "cache_uses": sum(v["cache_uses"] for v in merged.values()),
        "retries": sum(v["retries"] for v in merged.values()),
        "rate_limit_429": sum(v["rate_limit_429"] for v in merged.values()),
        "timeouts": sum(v["timeouts"] for v in merged.values()),
        "fallback_used": any(v["fallback_used"] for v in merged.values()),
        "lkg_used": any(v["lkg_used"] for v in merged.values()),
    }
    write_json(path, out)
    return out
