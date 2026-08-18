from pathlib import Path


def test_engine_metadata_describes_six_axis_and_independent_horizons():
    text = Path("src/kiee/engine.py").read_text(encoding="utf-8")
    assert '"current": "industry_six_axis_quality_weighted_with_observed_anchor"' in text
    assert '"future": "horizon_specific_3m_6m_12m_multi_source_models"' in text
    assert '"3_6m": "independent_6m_multi_source_model"' in text
    assert '"6_12m": "independent_12m_multi_source_model"' in text
    assert '"current_excludes_macro": False' in text


def test_quality_metadata_is_horizon_specific_and_backward_compatible():
    text = Path("src/kiee/engine.py").read_text(encoding="utf-8")
    assert 'quality["forecast_upstream_quality_by_horizon"]' in text
    assert '"3m": round(float(q3), 1)' in text
    assert '"3_6m": round(float(q6), 1)' in text
    assert '"6_12m": round(float(q12), 1)' in text
    assert 'quality["forecast_upstream_quality_score_basis"] = "3m_backward_compatibility"' in text
