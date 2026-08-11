from pathlib import Path

from kiee.krx_market import _calibrated_valuation_score, _write_valuation_history
from kiee.util import read_json


def test_sparse_history_neutralizes_cross_section_sector_bias():
    row = _calibrated_valuation_score(
        cross_section_score=10.0,
        current_per=60.0,
        current_pbr=8.0,
        history_rows=[],
        base_quality=90.0,
        policy={
            "valuation_history_min_samples": 8,
            "valuation_history_full_samples": 16,
            "valuation_provisional_cross_section_shrinkage": 0.20,
            "valuation_provisional_quality_cap": 35.0,
        },
    )
    # 10/100 raw cross-market relative valuation should not become 10/100 industry value
    # before the industry's own history exists: 50 + (10-50)*0.2 = 42.
    assert row["score"] == 42.0
    assert row["quality"] == 35.0
    assert row["history_ready"] is False
    assert row["method"] == "provisional_cross_section_shrunk"


def test_sufficient_own_history_becomes_primary_anchor():
    history = [
        {"median_per": 30 + i, "median_pbr": 2.0 + i * 0.05}
        for i in range(8)
    ]
    cheap = _calibrated_valuation_score(25.0, 20.0, 1.5, history, 90.0, {"valuation_history_min_samples": 8, "valuation_history_full_samples": 16})
    expensive = _calibrated_valuation_score(75.0, 55.0, 4.0, history, 90.0, {"valuation_history_min_samples": 8, "valuation_history_full_samples": 16})
    assert cheap["history_ready"] is True
    assert expensive["history_ready"] is True
    assert cheap["score"] > 50
    assert expensive["score"] < 50
    assert cheap["score"] > expensive["score"]


def test_valuation_history_is_one_snapshot_per_iso_week(tmp_path: Path):
    rows = {"semiconductor": {"median_per": 20.0, "median_pbr": 2.0}}
    _write_valuation_history(tmp_path, "20260810", rows)
    _write_valuation_history(tmp_path, "20260811", {"semiconductor": {"median_per": 21.0, "median_pbr": 2.1}})
    data = read_json(tmp_path / "output" / "validation" / "industry_valuation_history.json", {})
    assert len(data["snapshots"]) == 1
    assert data["snapshots"][0]["industries"]["semiconductor"]["median_per"] == 21.0
