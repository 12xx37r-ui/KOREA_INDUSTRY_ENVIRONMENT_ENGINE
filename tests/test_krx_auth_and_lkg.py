import json
from datetime import datetime, timezone
from pathlib import Path

from kiee.config import load_all
from kiee.krx_market import collect_sector_market
from kiee.util import read_json

ROOT = Path(__file__).resolve().parents[1]


def _sample_lkg(industries):
    rows = {}
    for industry in industries:
        rows[industry["key"]] = {
            "label": industry["label"],
            "requested_basket": list(industry.get("krx_basket") or []),
            "usable_members": list(industry.get("krx_basket") or [])[:2],
            "member_coverage": 1.0,
            "market_internal_score": 61.0,
            "market_internal_quality": 80.0,
            "valuation_score": 55.0,
            "valuation_quality": 70.0,
            "source_type": "direct_sector_basket",
        }
    return {
        "schema_version": "1.0.2",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "available": True,
        "industries": rows,
        "diagnostics": [],
    }


def test_missing_credentials_reuses_recent_lkg_without_network_calls(tmp_path, monkeypatch):
    industries_cfg, _, _ = load_all(ROOT)
    industries = industries_cfg["industries"]
    boom = read_json(ROOT / "fixtures" / "upstream" / "industry_boom_snapshot.json", {})
    cache = tmp_path / "input_cache" / "latest"
    cache.mkdir(parents=True)
    (cache / "krx_sector_market.json").write_text(json.dumps(_sample_lkg(industries), ensure_ascii=False), encoding="utf-8")
    monkeypatch.delenv("KRX_ID", raising=False)
    monkeypatch.delenv("KRX_PW", raising=False)
    result = collect_sector_market(tmp_path, industries, boom, stock_module=None, allow_live=True)
    assert result["available"] is True
    assert result["source_mode"] == "lkg-no-credentials"
    assert result["normal_live_calls"] == 0
    assert result["krx_credentials_configured"] is False


def test_missing_credentials_without_lkg_fails_fast_zero_calls(tmp_path, monkeypatch):
    industries_cfg, _, _ = load_all(ROOT)
    industries = industries_cfg["industries"]
    boom = read_json(ROOT / "fixtures" / "upstream" / "industry_boom_snapshot.json", {})
    monkeypatch.delenv("KRX_ID", raising=False)
    monkeypatch.delenv("KRX_PW", raising=False)
    result = collect_sector_market(tmp_path, industries, boom, stock_module=None, allow_live=True)
    assert result["available"] is False
    assert result["source_mode"] == "credentials-missing"
    assert result["normal_live_calls"] == 0
    assert any("krx_credentials_missing" in x for x in result["diagnostics"])
