from pathlib import Path

from kiee.prospective import update_registry


def _row(key, current, future, signal=0.5, q=80):
    return {
        "industry_key": key,
        "industry_label": key,
        "current": {"score": current},
        "forecast_3m": {"score": future, "quality_score": q},
        "stock_prediction_bridge": {"signal_normalized": signal},
    }


def test_forecast_registry_deduplicates_same_industry_month(tmp_path):
    direct = {"industries": {"semiconductor": {"member_closes": {"005930": 100, "000660": 100}}}}
    policy = {"prospective_min_cases": 24, "prospective_direction_accuracy_min": 0.55, "prospective_mean_return_spread_min_pct": 2.0}
    results = [_row("semiconductor", 60, 70)]
    a = update_registry(tmp_path, results, direct, policy, "2026-01-05T00:00:00+00:00")
    b = update_registry(tmp_path, results, direct, policy, "2026-01-25T00:00:00+00:00")
    assert a["registered_forecasts"] == 1
    assert b["registered_forecasts"] == 1
    assert b["status"] == "PENDING"
