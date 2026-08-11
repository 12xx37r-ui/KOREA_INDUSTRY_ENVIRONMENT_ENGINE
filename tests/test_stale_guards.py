from datetime import datetime, timedelta, timezone
from pathlib import Path

from kiee.krx_market import collect_sector_market
from kiee.upstream import UpstreamLoader
from kiee.util import write_json


def test_production_upstream_rejects_content_older_than_max_age(tmp_path):
    config = {
        "default_owner": "x",
        "sources": {"alpha": {"repo": "r", "branch": "main", "path": "x.json", "cache_ttl_hours": 1, "max_stale_hours": 12}},
    }
    loader = UpstreamLoader(tmp_path, config)
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    loader._fetch_github = lambda source: ({"generated_at": old, "value": 1}, 1)  # type: ignore[attr-defined]
    result = loader.load("alpha")
    assert result.ok is False
    assert result.stale is True
    assert "source content stale" in result.error


def test_expired_krx_lkg_is_not_reused(tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(hours=200)).isoformat()
    write_json(tmp_path / "input_cache" / "latest" / "krx_sector_market.json", {
        "generated_at_utc": old,
        "industries": {"semiconductor": {"market_internal_score": 80}},
    })
    result = collect_sector_market(
        tmp_path,
        [{"key": "semiconductor", "label": "반도체", "krx_basket": ["005930"], "theme_ids": []}],
        {},
        stock_module=None,
        allow_live=False,
        max_lkg_age_hours=120,
    )
    assert result["available"] is False
    assert result["source_mode"] == "unavailable"
    assert any("LKG expired" in x for x in result["diagnostics"])
