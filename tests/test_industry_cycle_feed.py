import json
from pathlib import Path

from kiee.industry_cycle_feed import build_feed


def _raw_row(score: float, factor: str, source: str = "KOSIS:TEST"):
    return {
        "id": factor,
        "factor": factor,
        "value": score,
        "unit": "index",
        "long_run_percentile": score,
        "quality": 90,
        "source": source,
        "as_of": "2026-08-12",
    }


def _copy_config(tmp_path: Path):
    (tmp_path / "config").mkdir()
    (tmp_path / "input").mkdir()
    for name in ("industries.json", "industry_universe.json", "scoring_policy.json", "upstreams.json"):
        source = Path(__file__).parents[1] / "config" / name
        (tmp_path / "config" / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def test_builder_calculates_only_from_available_factor_metrics(tmp_path: Path):
    _copy_config(tmp_path)
    raw = {
        "schema_version": "1.0.0", "status": "raw", "generated_at_utc": "2026-08-12T00:00:00+00:00",
        "industries": [{
            "industry_key": "semiconductor",
            "current": {"metrics": [_raw_row(80, "production_shipments"), _raw_row(70, "sales_earnings"), _raw_row(60, "inventory_cycle"), _raw_row(55, "utilization")]},
            "forecasts": {"3m": {"metrics": [_raw_row(75, "new_orders"), _raw_row(70, "inventory_cycle"), _raw_row(65, "global_environment"), _raw_row(60, "rates_liquidity")]}, "3_6m": {}, "6_12m": {}},
        }],
    }
    (tmp_path / "input" / "industry_cycle_raw.json").write_text(json.dumps(raw), encoding="utf-8")
    result = build_feed(tmp_path)
    row = result["industries"][0]
    assert row["current"]["status"] == "scored"
    assert row["current"]["score"] is not None
    assert row["forecasts"]["3m"]["status"] == "scored"
    assert row["forecasts"]["3m"]["score"] is not None


def test_builder_keeps_pending_when_raw_input_is_missing(tmp_path: Path):
    _copy_config(tmp_path)
    result = build_feed(tmp_path)
    assert result["status"] == "pending"
    assert result["industries"] == []
