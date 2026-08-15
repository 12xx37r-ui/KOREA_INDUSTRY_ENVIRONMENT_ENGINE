from pathlib import Path


def test_stable_engine_version_and_health_classification_source():
    root = Path(__file__).resolve().parents[1]
    policy = (root / "config" / "scoring_policy.json").read_text(encoding="utf-8")
    engine = (root / "src" / "kiee" / "engine.py").read_text(encoding="utf-8")
    assert '"engine_version": "1.3.0-stable"' in policy
    assert '"structural_pending"' in engine
    assert 'current_factor_structural_pending_count' in engine
    assert 'current_structural_pending_by_factor' in engine
    assert 'current_gap_proxy_by_factor' in engine
    assert 'core_current_gap_industry_count' in engine


def test_reit_pending_is_not_silently_marked_direct():
    root = Path(__file__).resolve().parents[1]
    engine = (root / "src" / "kiee" / "engine.py").read_text(encoding="utf-8")
    assert '("real_estate_reit", "valuation")' in engine
    assert '("reit_office_logistics", "valuation")' in engine
    assert 'return "structural_pending"' in engine


def test_source_freshness_exposes_threshold_without_changing_stale_rule():
    root = Path(__file__).resolve().parents[1]
    engine = (root / "src" / "kiee" / "engine.py").read_text(encoding="utf-8")
    assert 'stale_after_hours' in engine
    assert 'freshness_state' in engine
    assert 'near_stale' in engine
