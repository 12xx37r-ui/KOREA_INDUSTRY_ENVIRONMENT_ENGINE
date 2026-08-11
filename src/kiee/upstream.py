from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .util import age_hours, read_json, utc_now_iso, write_json


@dataclass
class SourceResult:
    name: str
    payload: dict[str, Any] | None
    ok: bool
    mode: str
    error: str = ""
    stale: bool = False
    age_hours: float | None = None
    http_calls: int = 0
    source_generated_at: str = ""
    source_age_hours: float | None = None


class UpstreamLoader:
    """Load each upstream exactly once per engine run, with committed LKG fallback."""

    def __init__(self, root: Path, config: dict[str, Any], fixture_dir: Path | None = None, timeout: int = 20):
        self.root = root
        self.config = config
        self.fixture_dir = fixture_dir
        self.timeout = timeout
        self.memo: dict[str, SourceResult] = {}
        self.http_calls = 0
        self.owner = os.getenv(str(config.get("owner_env") or "UPSTREAM_GITHUB_OWNER"), str(config.get("default_owner") or "12xx37r-ui"))
        self.token = os.getenv(str(config.get("token_env") or "UPSTREAM_GITHUB_TOKEN"), "").strip()
        self.cache_dir = root / "input_cache" / "latest"

    def load_all(self) -> dict[str, SourceResult]:
        return {name: self.load(name) for name in (self.config.get("sources") or {})}

    def load(self, name: str) -> SourceResult:
        if name in self.memo:
            return self.memo[name]
        source = (self.config.get("sources") or {}).get(name)
        if not isinstance(source, dict):
            result = SourceResult(name, None, False, "config-error", f"unknown source: {name}")
            self.memo[name] = result
            return result

        if self.fixture_dir:
            fixture_map = {
                "korea_rate_fx": "korea_rate_fx_outlook_v3.json",
                "korea_equity": "korea_equity_environment.json",
                "global_bundle": "cards_8_12_bundle.json",
                "fed_futures": "fed_futures_latest.json",
                "industry_boom": "industry_boom_snapshot.json",
                "industry_cycle": "industry_cycle_latest.json",
            }
            path = self.fixture_dir / fixture_map.get(name, f"{name}.json")
            payload = read_json(path)
            result = self._result_from_payload(name, payload, "fixture", 0, "")
            self.memo[name] = result
            return result

        local_path = str(source.get("local_path") or "").strip()
        if local_path:
            payload = read_json(self.root / local_path)
            if isinstance(payload, dict) and payload:
                result = self._result_from_payload(name, payload, "local", 0, "")
                self.memo[name] = result
                return result

        cache_path = self.cache_dir / f"{name}.json"
        meta_path = self.cache_dir / f"{name}.meta.json"
        cached = read_json(cache_path)
        meta = read_json(meta_path, {}) or {}
        cache_age = age_hours(meta.get("fetched_at"))
        ttl = float(source.get("cache_ttl_hours") or 0)
        if isinstance(cached, dict) and cache_age is not None and cache_age <= ttl:
            result = self._result_from_payload(name, cached, "cache-fresh", 0, "", stale=False)
            result.age_hours = cache_age
            self.memo[name] = result
            return result

        payload = None
        error = ""
        calls = 0
        try:
            payload, calls = self._fetch_github(source)
        except Exception as exc:  # bounded fallback below
            error = f"{type(exc).__name__}: {exc}"
        self.http_calls += calls

        if isinstance(payload, dict):
            write_json(cache_path, payload)
            write_json(meta_path, {
                "name": name,
                "fetched_at": utc_now_iso(),
                "owner": self.owner,
                "repo": source.get("repo"),
                "branch": source.get("branch"),
                "path": source.get("path"),
                "mode": "github-api" if self.token else "raw-public",
            })
            result = self._result_from_payload(name, payload, "github-api" if self.token else "raw-public", calls, "")
            self.memo[name] = result
            return result

        max_stale = float(source.get("max_stale_hours") or 0)
        if isinstance(cached, dict) and cache_age is not None and cache_age <= max_stale:
            result = self._result_from_payload(name, cached, "cache-stale-fallback", calls, error, stale=True)
            result.age_hours = cache_age
            self.memo[name] = result
            return result

        result = SourceResult(name, None, False, "failed", error or "upstream unavailable", http_calls=calls)
        self.memo[name] = result
        return result

    def _fetch_github(self, source: dict[str, Any]) -> tuple[dict[str, Any], int]:
        repo = str(source.get("repo") or "").strip()
        branch = str(source.get("branch") or "main").strip()
        path = str(source.get("path") or "").strip().lstrip("/")
        if not repo or not path:
            raise RuntimeError("repo/path missing")
        calls = 0
        if self.token:
            api = (
                "https://api.github.com/repos/" + urllib.parse.quote(self.owner) + "/" + urllib.parse.quote(repo)
                + "/contents/" + "/".join(urllib.parse.quote(part) for part in path.split("/"))
                + "?ref=" + urllib.parse.quote(branch)
            )
            request = urllib.request.Request(api, headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "korea-industry-environment-engine/1.0",
            })
            calls += 1
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            content = body.get("content") if isinstance(body, dict) else None
            if not content:
                raise RuntimeError("GitHub Contents API content missing")
            decoded = base64.b64decode(str(content).replace("\n", "")).decode("utf-8")
            payload = json.loads(decoded)
            if not isinstance(payload, dict):
                raise RuntimeError("upstream JSON root is not object")
            return payload, calls

        raw = (
            "https://raw.githubusercontent.com/" + urllib.parse.quote(self.owner) + "/" + urllib.parse.quote(repo)
            + "/" + urllib.parse.quote(branch) + "/" + "/".join(urllib.parse.quote(part) for part in path.split("/"))
        )
        request = urllib.request.Request(raw, headers={"User-Agent": "korea-industry-environment-engine/1.0"})
        calls += 1
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("upstream JSON root is not object")
        return payload, calls

    @staticmethod
    def _source_generated_at(name: str, payload: dict[str, Any]) -> str:
        candidates = [
            payload.get("generated_at"), payload.get("generated_at_utc"), payload.get("as_of"),
            payload.get("generatedAt"), payload.get("snapshot_id"),
        ]
        if name == "industry_boom":
            candidates.insert(0, payload.get("as_of"))
        for value in candidates:
            if value:
                return str(value)
        return ""

    def _result_from_payload(self, name: str, payload: Any, mode: str, calls: int, error: str, stale: bool = False) -> SourceResult:
        ok = isinstance(payload, dict) and bool(payload)
        generated = self._source_generated_at(name, payload if isinstance(payload, dict) else {})
        source_age = age_hours(generated)
        source_cfg = (self.config.get("sources") or {}).get(name) or {}
        max_content_age = float(source_cfg.get("max_stale_hours") or 0.0)
        content_too_old = bool(mode != "fixture" and source_age is not None and max_content_age > 0 and source_age > max_content_age)
        if content_too_old:
            stale = True
            ok = False
            age_note = f"source content stale: {source_age:.1f}h > {max_content_age:.1f}h"
            error = (error + " · " + age_note).strip(" ·") if error else age_note
        return SourceResult(
            name=name,
            payload=payload if isinstance(payload, dict) and bool(payload) else None,
            ok=ok,
            mode=(mode + "-content-stale") if content_too_old else mode,
            error=error,
            stale=stale,
            http_calls=calls,
            source_generated_at=generated,
            source_age_hours=source_age,
        )
