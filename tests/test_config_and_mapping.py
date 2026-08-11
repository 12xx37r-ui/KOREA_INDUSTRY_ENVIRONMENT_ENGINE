from pathlib import Path

from kiee.config import load_all

ROOT = Path(__file__).resolve().parents[1]


def test_industry_universe_and_weights():
    industries_cfg, policy, upstreams = load_all(ROOT)
    rows = industries_cfg["industries"]
    assert len(rows) == 25
    keys = {r["key"] for r in rows}
    required = {
        "semiconductor", "electronic_components", "automotive", "battery",
        "shipbuilding", "construction", "finance", "insurance",
        "biotechnology", "medical_devices", "software_platform",
        "media_entertainment", "retail", "utilities_power",
    }
    assert required <= keys
    for row in rows:
        assert abs(sum(row["weights_current"].values()) - 1.0) < 1e-10
        assert abs(sum(row["weights_3m"].values()) - 1.0) < 1e-10
    assert policy["forecast_primary_use_allowed"] is False
    assert upstreams["normal_upstream_http_calls_per_run"] == 4


def test_stock_engine_alias_compatibility():
    industries_cfg, _, _ = load_all(ROOT)
    aliases = {}
    for row in industries_cfg["industries"]:
        aliases[row["key"]] = row["key"]
        aliases[row["label"]] = row["key"]
        for alias in row.get("aliases", []):
            aliases[alias] = row["key"]
    assert aliases["media_entertainment"] == "media_entertainment"
    assert aliases["미디어·엔터테인먼트·콘텐츠"] == "media_entertainment"
    assert aliases["shipbuilding"] == "shipbuilding"
    assert aliases["조선·해양·선박"] == "shipbuilding"
    assert aliases["electronic_components"] == "electronic_components"
    assert aliases["software_platform"] == "software_platform"
    assert aliases["medical_devices"] == "medical_devices"
