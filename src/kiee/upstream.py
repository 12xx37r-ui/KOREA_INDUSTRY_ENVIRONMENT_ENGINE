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
from .api_health import record_cache, record_fallback, record_lkg


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
        # Freshness-first: perform a conditional source check every workflow even
        # when the local TTL has not expired. A 304 is an intentional CACHE use;
        # an unchecked TTL hit is not considered sufficiently fresh.

        # Attempt conditional GET first (ETag / Last-Modified).
        # A 304 response means content is unchanged: refresh fetched_at and reuse
        # the cached payload without re-downloading the file.
        # _pending_meta is read by _fetch_github to build conditional request headers;
        # keeping the public signature as (source) preserves compatibility with mocks.
        self._pending_meta: dict[str, Any] = meta
        payload = None
        error = ""
        calls = 0
        not_modified = False
        try:
            result_tuple = self._fetch_github(source)
            # Support both old mock signature (payload, calls) and new (payload, calls, not_modified).
            if len(result_tuple) == 3:
                payload, calls, not_modified = result_tuple
            else:
                payload, calls = result_tuple
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        self.http_calls += calls

        if not_modified and isinstance(cached, dict) and cached:
            # Content unchanged — extend TTL by refreshing fetched_at only.
            new_meta = dict(meta)
            new_meta["fetched_at"] = utc_now_iso()
            new_meta["mode"] = "conditional-not-modified"
            write_json(meta_path, new_meta)
            record_cache("GITHUB")
            result = self._result_from_payload(name, cached, "cache-not-modified", calls, "", stale=False)
            result.age_hours = 0.0
            self.memo[name] = result
            return result

        if isinstance(payload, dict):
            write_json(cache_path, payload)
            # Persist etag/last_modified returned by this fetch for next conditional GET.
            new_meta = {
                "name": name,
                "fetched_at": utc_now_iso(),
                "owner": self.owner,
                "repo": source.get("repo"),
                "branch": source.get("branch"),
                "path": source.get("path"),
                "mode": "github-api" if self.token else "raw-public",
            }
            if isinstance(payload, dict):
                # Carry forward any etag/last_modified set during _fetch_github
                for key in ("etag", "last_modified"):
                    value = getattr(self, f"_last_{key}", None)
                    if value:
                        new_meta[key] = value
            write_json(meta_path, new_meta)
            result = self._result_from_payload(name, payload, "github-api" if self.token else "raw-public", calls, "")
            self.memo[name] = result
            return result

        max_stale = float(source.get("max_stale_hours") or 0)
        if isinstance(cached, dict) and cache_age is not None and cache_age <= max_stale:
            record_lkg("GITHUB")
            result = self._result_from_payload(name, cached, "cache-stale-fallback", calls, error, stale=True)
            result.age_hours = cache_age
            self.memo[name] = result
            return result

        result = SourceResult(name, None, False, "failed", error or "upstream unavailable", http_calls=calls)
        self.memo[name] = result
        return result

    def _fetch_github(self, source: dict[str, Any]) -> tuple[dict[str, Any] | None, int, bool]:
        """Fetch from GitHub. Returns (payload, calls, not_modified).

        payload is None and not_modified is True when the server returns HTTP 304,
        meaning the cached content is still current and should be reused.
        Reads self._pending_meta (set by load()) for ETag / Last-Modified headers
        so the public signature remains (source,) for backwards-compatible mocking.
        """
        repo = str(source.get("repo") or "").strip()
        branch = str(source.get("branch") or "main").strip()
        path = str(source.get("path") or "").strip().lstrip("/")
        if not repo or not path:
            raise RuntimeError("repo/path missing")
        meta = getattr(self, "_pending_meta", None) or {}
        calls = 0

        # Reset per-fetch header capture
        self._last_etag: str = ""
        self._last_last_modified: str = ""

        if self.token:
            api = (
                "https://api.github.com/repos/" + urllib.parse.quote(self.owner) + "/" + urllib.parse.quote(repo)
                + "/contents/" + "/".join(urllib.parse.quote(part) for part in path.split("/"))
                + "?ref=" + urllib.parse.quote(branch)
            )
            headers: dict[str, str] = {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "korea-industry-environment-engine/1.0",
            }
            if meta.get("etag"):
                headers["If-None-Match"] = str(meta["etag"])
            request = urllib.request.Request(api, headers=headers)
            calls += 1
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    self._last_etag = response.headers.get("ETag", "")
                    body = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 304:
                    return None, calls, True
                raise
            content = body.get("content") if isinstance(body, dict) else None
            if not content:
                raise RuntimeError("GitHub Contents API content missing")
            decoded = base64.b64decode(str(content).replace("\n", "")).decode("utf-8")
            payload = json.loads(decoded)
            if not isinstance(payload, dict):
                raise RuntimeError("upstream JSON root is not object")
            return payload, calls, False

        raw = (
            "https://raw.githubusercontent.com/" + urllib.parse.quote(self.owner) + "/" + urllib.parse.quote(repo)
            + "/" + urllib.parse.quote(branch) + "/" + "/".join(urllib.parse.quote(part) for part in path.split("/"))
        )
        raw_headers: dict[str, str] = {"User-Agent": "korea-industry-environment-engine/1.0"}
        if meta.get("etag"):
            raw_headers["If-None-Match"] = str(meta["etag"])
        elif meta.get("last_modified"):
            raw_headers["If-Modified-Since"] = str(meta["last_modified"])
        request = urllib.request.Request(raw, headers=raw_headers)
        calls += 1
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                self._last_etag = response.headers.get("ETag", "")
                self._last_last_modified = response.headers.get("Last-Modified", "")
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return None, calls, True
            raise
        if not isinstance(payload, dict):
            raise RuntimeError("upstream JSON root is not object")
        return payload, calls, False

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
