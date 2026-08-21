from pathlib import Path
from kiee.prospective import update_registry


def test_registry_adds_challenger_fields_without_replacing_legacy(tmp_path: Path):
    (tmp_path / "output" / "validation").mkdir(parents=True)
    rows = [{
        "industry_key": "x", "industry_label": "X",
        "current": {"score": 55},
        "forecast_3m": {"score": 60, "score_v2_shadow": 62, "quality_score": 70},
        "forecast_3_6m": {"score": 61, "score_v2_shadow": 63},
        "forecast_6_12m": {"score": 62, "score_v2_shadow": 64},
        "stock_prediction_bridge": {"signal_normalized": 0.2},
    }]
    direct = {"industries": {"x": {"member_closes": {"A": 100, "B": 100}}}}
    policy = {"prospective_min_cases": 24, "prospective_direction_accuracy_min": 0.55, "prospective_mean_return_spread_min_pct": 2.0}
    summary = update_registry(tmp_path, rows, direct, policy, "2026-08-21T00:00:00+00:00")
    assert summary["challenger_v2"]["production_score_unchanged"] is True
    import json
    reg = json.loads((tmp_path / "output" / "validation" / "forecast_registry.json").read_text())
    e = reg["entries"][0]
    assert e["forecast_3m_score"] == 60
    assert e["forecast_3m_v2_shadow_score"] == 62
    assert e["forecast_6_12m_v2_shadow_score"] == 64
