from kiee.engine import _reconcile_six_axis_outputs


def _factor(score, quality, proxy=False):
    return {
        "score": score,
        "quality": quality,
        "available": True,
        "proxy": proxy,
        "source": "test",
        "detail": "test",
    }


def _policy():
    return {
        "score_bands": [
            {"min": 0, "max": 42, "label": "불리"},
            {"min": 43, "max": 57, "label": "중립"},
            {"min": 58, "max": 69, "label": "약우호"},
            {"min": 70, "max": 100, "label": "우호"},
        ],
        "delta_bands": [
            {"abs_max": 1, "label": "거의 변화 없음"},
            {"abs_max": 3, "label": "조금"},
            {"abs_max": 8, "label": "보통"},
            {"abs_max": 999, "label": "많이"},
        ],
    }


def test_current_headline_reconciles_to_displayed_six_axis_aggregate():
    industries = [{
        "key": "industrial_machinery",
        "weights_current": {
            "earnings_momentum": 0.20,
            "demand_cycle": 0.30,
            "pricing_margin": 0.15,
            "financial_conditions": 0.12,
            "market_internals": 0.13,
            "valuation": 0.10,
        },
        "weights_3m": {
            "earnings_momentum": 0.16,
            "demand_cycle": 0.35,
            "pricing_margin": 0.14,
            "financial_conditions": 0.12,
            "market_internals": 0.17,
            "valuation": 0.06,
        },
        "sensitivities": {
            "rate_relief": 0.4, "krw_weakness": 0.5, "liquidity": 0.4,
            "credit_health": 0.2, "consumer_cycle": 0.6, "cost_relief": 0.3,
        },
    }]
    row = {
        "industry_key": "industrial_machinery",
        "current": {
            "score": 66.9,
            "band": "약우호",
            "quality_score": 35.0,
            "data_coverage_pct": 10.0,
            "quality_weighted_coverage_pct": 3.5,
            "observed_coverage_pct": 10.0,
            "status": "scored",
            "score_source": "observed",
            "factors": {
                "earnings_momentum": _factor(58.08, 60.84),
                "demand_cycle": _factor(79.06, 81.1),
                "pricing_margin": _factor(54.32, 59.91),
                "financial_conditions": _factor(41.61, 86.2),
                "market_internals": _factor(60.75, 91.2),
                "valuation": _factor(40.27, 35.0),
            },
        },
        "forecast_3m": {"score": 50.1, "score_source": "estimated"},
        "forecast_3_6m": {"score": 50.1, "score_source": "estimated"},
        "forecast_6_12m": {"score": 50.1, "score_source": "estimated"},
        "quality": {"current_metric_coverage": 10.0},
        "model": {"current": "industry_observed_metrics_only"},
        "interpretation": {"headline": "현재 약우호 67/100", "beginner": "old"},
    }

    korea_rate = {
        "rate": {"current_rate_pct": 3.0, "calendar_horizon_estimates": {"6m": 2.75, "12m": 2.5}, "quality_gate": {"forecast_quality_score": 84}},
        "fx": {"current_usdkrw": 1400, "forecast_path": [
            {"months": 6, "point_forecast": 1370, "model_quality_score": 70},
            {"months": 12, "point_forecast": 1340, "model_quality_score": 68},
        ]},
        "krw_liquidity": {"current": {"liquidity_score": 0.0}, "forecast_path": [
            {"months": 6, "liquidity_score": 0.2, "forecast_quality_score": 85},
            {"months": 12, "liquidity_score": 0.4, "forecast_quality_score": 82},
        ]},
        "krw_strength": {"current": {"strength_score": 50}, "forecast_path": [
            {"months": 6, "strength_score": 55, "independent_oos_quality_score": 78},
            {"months": 12, "strength_score": 60, "independent_oos_quality_score": 76},
        ]},
    }
    korea_equity = {"components": {"credit_spread": {"score_normalized": 0.2}}, "current_inputs": {"credit": {"gov_3y_pct": 3.2}}}
    global_bundle = {"cards": {
        "9": {"current": 50, "forecasts": {
            "6m": {"forecast": 58, "quality_gate": {"passed": True}},
            "12m": {"forecast": 64, "quality_gate": {"passed": True}},
        }},
        "10": {"current": 50, "forecasts": {
            "6m": {"forecast": 46, "quality_gate": {"passed": True}},
            "12m": {"forecast": 40, "quality_gate": {"passed": True}},
        }},
    }}
    out = _reconcile_six_axis_outputs([row], industries, _policy(), korea_rate, korea_equity, global_bundle)[0]
    cur = out["current"]
    assert 60.0 <= cur["score"] <= 63.0
    assert cur["data_coverage_pct"] == 100.0
    assert cur["quality_weighted_coverage_pct"] > 65.0
    assert cur["quality_score"] > 65.0
    assert cur["score_source"] == "observed"  # backward-compatible key semantics
    assert cur["score_basis"] == "six_axis_quality_weighted_aggregate"
    assert cur["legacy_observed_anchor_score"] == 66.9
    assert out["forecast_3m"]["delta_points"] == round(50.1 - cur["score"], 1)
    assert out["forecast_3_6m"]["horizon_specific_inputs"] is True
    assert out["forecast_6_12m"]["horizon_specific_inputs"] is True
    assert out["forecast_3_6m"]["horizon_basis"] == "independent_6m_multi_source_model"
    assert out["forecast_6_12m"]["horizon_basis"] == "independent_12m_multi_source_model"
    assert out["forecast_3_6m"]["score"] != out["forecast_6_12m"]["score"]
    assert out["forecast_3_6m"]["horizon_inputs"]["global_consumer_forecast"] == 58
    assert out["forecast_6_12m"]["horizon_inputs"]["global_consumer_forecast"] == 64
    assert out["quality"]["observed_metric_coverage_pct"] == 10.0
    assert out["quality"]["current_six_axis_coverage_pct"] == 100.0


def test_single_axis_feed_only_result_is_not_rewritten():
    industries = [{"key": "x", "weights_current": {"demand_cycle": 1.0}}]
    row = {
        "industry_key": "x",
        "current": {
            "score": 61.0,
            "quality_score": 35.0,
            "data_coverage_pct": 10.0,
            "observed_coverage_pct": 10.0,
            "status": "scored",
            "factors": {"demand_cycle": _factor(61.0, 85.0)},
        },
    }
    out = _reconcile_six_axis_outputs([row], industries, _policy())[0]
    assert out["current"]["score"] == 61.0
    assert "score_basis" not in out["current"]
