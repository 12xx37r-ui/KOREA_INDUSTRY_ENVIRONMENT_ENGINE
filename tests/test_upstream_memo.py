import json
from pathlib import Path
from datetime import datetime, timezone

from kiee.upstream import UpstreamLoader


def test_same_upstream_is_loaded_once_in_same_run(tmp_path):
    config = {
        "default_owner": "x",
        "sources": {"alpha": {"repo": "r", "branch": "main", "path": "x.json", "cache_ttl_hours": 0, "max_stale_hours": 24}},
    }
    loader = UpstreamLoader(tmp_path, config)
    calls = {"n": 0}

    def fake_fetch(source):
        calls["n"] += 1
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "value": 1}, 1

    loader._fetch_github = fake_fetch  # type: ignore[attr-defined]
    a = loader.load("alpha")
    b = loader.load("alpha")
    assert a.ok and b.ok
    assert calls["n"] == 1
    assert loader.http_calls == 1
