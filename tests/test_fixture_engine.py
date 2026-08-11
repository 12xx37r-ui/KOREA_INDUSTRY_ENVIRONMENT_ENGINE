import json
import shutil
from pathlib import Path

from kiee.engine import run_engine

SOURCE_ROOT = Path(__file__).resolve().parents[1]


def _runtime(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    for folder in ("config", "fixtures"):
        shutil.copytree(SOURCE_ROOT / folder, root / folder)
    (root / "output" / "industries").mkdir(parents=True)
    (root / "input_cache" / "latest").mkdir(parents=True)
    return root


def test_actual_user_fixtures_produce_25_bounded_industries(tmp_path):
    root = _runtime(tmp_path)
    result = run_engine(root, fixture_dir=root / "fixtures" / "upstream", allow_live_krx=False)
    assert result["status"] == "ok"
    assert result["industry_count"] >= 100
    assert result["call_efficiency"]["upstream_http_calls_this_run"] == 0
    assert result["call_efficiency"]["krx_bulk_calls_this_run"] == 0
    assert result["call_efficiency"]["per_company_live_calls"] == 0
    for row in result["industries"]:
        for block in ("current", "forecast_3m"):
            assert 0 <= row[block]["score"] <= 100
            assert 0 <= row[block]["quality_score"] <= 100
            assert set(row[block]["factors"].keys()) == {
                "earnings_momentum", "demand_cycle", "pricing_margin",
                "financial_conditions", "market_internals", "valuation",
            }
        assert abs(row["stock_prediction_bridge"]["bounded_direction_adjustment_points"]) <= 5.0001
        assert row["stock_prediction_bridge"]["allowed_as_primary"] is False


def test_missing_industry_cycle_feed_does_not_impute_scores(tmp_path):
    root = _runtime(tmp_path)
    result = run_engine(root, fixture_dir=root / "fixtures" / "upstream", allow_live_krx=False)
    for row in result["industries"]:
        assert row["current"]["score"] is None
        assert row["forecast_3m"]["score"] is None
        assert row["forecast_3_6m"]["score"] is None
        assert row["forecast_6_12m"]["score"] is None
        assert row["quality"]["data_status"] == "insufficient_data"


def test_media_does_not_invent_industry_boom_theme(tmp_path):
    root = _runtime(tmp_path)
    result = run_engine(root, fixture_dir=root / "fixtures" / "upstream", allow_live_krx=False)
    row = next(r for r in result["industries"] if r["industry_key"] == "media_entertainment")
    assert row["theme_bridge"]["available"] is False
    assert row["current"]["factors"]["earnings_momentum"]["proxy"] is True


def test_prevalidation_theme_is_shrunk_and_quality_capped(tmp_path):
    root = _runtime(tmp_path)
    result = run_engine(root, fixture_dir=root / "fixtures" / "upstream", allow_live_krx=False)
    assert all(row["current"]["score"] is None for row in result["industries"])


def test_compact_bridge_is_written(tmp_path):
    root = _runtime(tmp_path)
    result = run_engine(root, fixture_dir=root / "fixtures" / "upstream", allow_live_krx=False)
    bridge = json.loads((root / "output" / "stock_prediction_bridge.json").read_text(encoding="utf-8"))
    assert len(bridge["by_profile_key"]) == result["industry_count"]
    assert bridge["alias_to_profile_key"]["미디어·엔터테인먼트·콘텐츠"] == "media_entertainment"
    assert bridge["alias_to_profile_key"]["조선·해양·선박"] == "shipbuilding"


def test_narrow_boom_theme_cannot_masquerade_as_bank_sector(tmp_path):
    root = _runtime(tmp_path)
    result = run_engine(root, fixture_dir=root / "fixtures" / "upstream", allow_live_krx=False)
    for key in ("finance", "insurance", "securities"):
        row = next(r for r in result["industries"] if r["industry_key"] == key)
        assert row["theme_bridge"]["available"] is False
        assert row["theme_bridge"]["relevance_weight"] == 0.0
