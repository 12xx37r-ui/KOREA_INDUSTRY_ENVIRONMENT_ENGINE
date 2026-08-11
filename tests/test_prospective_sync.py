import shutil
from pathlib import Path

import kiee.engine as engine_mod
from kiee.config import load_all

SOURCE_ROOT = Path(__file__).resolve().parents[1]


def _runtime(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    for folder in ("config", "fixtures"):
        shutil.copytree(SOURCE_ROOT / folder, root / folder)
    (root / "output" / "industries").mkdir(parents=True)
    (root / "input_cache" / "latest").mkdir(parents=True)
    return root


def test_per_industry_prospective_metadata_matches_just_updated_registry(tmp_path, monkeypatch):
    root = _runtime(tmp_path)
    industries_cfg, _, _ = load_all(root)
    direct_rows = {}
    for i, industry in enumerate(industries_cfg["industries"]):
        code1 = f"{100000+i*2:06d}"[-6:]
        code2 = f"{100001+i*2:06d}"[-6:]
        direct_rows[industry["key"]] = {
            "market_internal_score": 50.0,
            "market_internal_quality": 80.0,
            "valuation_score": 50.0,
            "valuation_quality": 35.0,
            "valuation_history_ready": False,
            "valuation_history_samples": 0,
            "valuation_method": "provisional_cross_section_shrunk",
            "member_closes": {code1: 100.0, code2: 101.0},
        }

    monkeypatch.setattr(engine_mod, "collect_sector_market", lambda *a, **k: {
        "available": True,
        "source_mode": "test",
        "normal_live_calls": 0,
        "normal_target_calls": 8,
        "industries": direct_rows,
        "industry_coverage_pct": 100.0,
        "fresh_industry_count": 25,
        "lkg_reused_industry_count": 0,
        "available_industry_count": 25,
        "valuation_history_ready_industry_count": 0,
        "valuation_history_total_industry_count": 25,
    })

    result = engine_mod.run_engine(root, fixture_dir=root / "fixtures" / "upstream", allow_live_krx=False)
    expected = result["prospective_validation"]["registered_forecasts"]
    assert expected == 25
    for row in result["industries"]:
        embedded = row["quality"]["prospective_validation"]
        assert embedded["registered_forecasts"] == expected
        assert row["stock_prediction_bridge"]["validation_status"] == result["prospective_validation"]["status"]
