from copy import deepcopy

from kiee.engine import _reconcile_six_axis_outputs


def _factor(score, quality=60.0):
    return {"score": score, "quality": quality, "available": True, "proxy": False}


def test_estimated_industry_missing_long_horizons_are_filled_from_existing_upstreams():
    industry = {
        "key": "sample",
        "weights_3m": {
            "earnings_momentum": 0.2,
            "demand_cycle": 0.25,
            "pricing_margin": 0.15,
            "financial_conditions": 0.15,
            "market_internals": 0.15,
            "valuation": 0.10,
        },
        "sensitivities": {"consumer_cycle": 0.7, "cost_relief": 0.4, "rate_relief": 0.5, "krw_weakness": 0.2, "liquidity": 0.3, "credit_health": 0.2},
    }
    row = {
        "industry_key": "sample",
        "current": {"score": 40.0, "status": "estimated"},
        "forecast_3m": {
            "score": 48.0,
            "quality_score": 33.0,
            "factors": {
                "earnings_momentum": _factor(55),
                "demand_cycle": _factor(45),
                "pricing_margin": _factor(52),
                "financial_conditions": _factor(44, 75),
                "market_internals": _factor(58, 70),
                "valuation": _factor(47, 35),
            },
        },
        "forecast_3_6m": {"score": None, "status": "insufficient_data"},
        "forecast_6_12m": {"score": None, "status": "insufficient_data"},
        "quality": {},
        "score_model": {"current": "estimated_macro_theme_krx", "future": "estimated_macro_theme_financial_conditions"},
    }
    korea_rate = {
        "rate": {"current_rate_pct": 2.75, "calendar_horizon_estimates": {"6m": 2.6, "12m": 2.5}, "quality_gate": {"forecast_quality_score": 70}},
        "fx": {"current_usdkrw": 1380, "forecast_path": [{"months": 6, "point_forecast": 1370, "model_quality_score": 65}, {"months": 12, "point_forecast": 1360, "model_quality_score": 60}]},
        "krw_liquidity": {"current": {"liquidity_score": 0.1}, "forecast_path": [{"months": 6, "liquidity_score": 0.15, "forecast_quality_score": 70}, {"months": 12, "liquidity_score": 0.2, "forecast_quality_score": 65}]},
        "krw_strength": {"current": {"strength_score": 60}, "forecast_path": [{"months": 6, "strength_score": 62, "model_quality_score": 65}, {"months": 12, "strength_score": 64, "model_quality_score": 60}]},
    }
    korea_equity = {"components": {"credit_spread": {"score_normalized": 0.1}}, "current_inputs": {"credit": {"gov_3y_pct": 3.0}}}
    global_bundle = {
        "cards": {
            "9": {"current": 50, "forecasts": {"6m": {"forecast": 52, "quality_gate": {"passed": True}}, "12m": {"forecast": 54, "quality_gate": {"candidate": True}}}},
            "10": {"current": 50, "forecasts": {"6m": {"forecast": 49, "quality_gate": {"passed": True}}, "12m": {"forecast": 48, "quality_gate": {"candidate": True}}}},
        }
    }

    out = _reconcile_six_axis_outputs([deepcopy(row)], [industry], {}, korea_rate, korea_equity, global_bundle, {})[0]

    assert out["forecast_3_6m"]["score"] is not None
    assert out["forecast_6_12m"]["score"] is not None
    assert out["forecast_3_6m"]["score_source"] == "independent_horizon_model"
    assert out["forecast_6_12m"]["score_source"] == "independent_horizon_model"
    assert out["forecast_3_6m"]["filled_from_existing_upstreams"] is True
    assert out["forecast_6_12m"]["filled_from_existing_upstreams"] is True
    assert out["score_model"]["horizon_specific_models"]["3_6m"] == "independent_6m_multi_source_model"
    assert out["quality"]["forecast_upstream_quality_by_horizon"]["6_12m"] is not None


def test_estimated_forecast_quality_uses_information_density_not_fixed_45_cap():
    from kiee.scoring import _build_estimated_forecast

    # Six direct factors with ~60 quality and full coverage should naturally clear 50,
    # while remaining below observed-quality territory due to the estimated haircut.
    industry = {
        "key": "sample",
        "weights_3m": {
            "earnings_momentum": 1/6, "demand_cycle": 1/6, "pricing_margin": 1/6,
            "financial_conditions": 1/6, "market_internals": 1/6, "valuation": 1/6,
        },
        "sensitivities": {},
    }
    # Patch factor construction so the test isolates the quality formula.
    import kiee.scoring as scoring
    factors = {k: {"score": 50.0, "quality": q, "available": True, "proxy": False} for k, q in {
        "earnings_momentum": 51, "demand_cycle": 52, "pricing_margin": 74,
        "financial_conditions": 83, "market_internals": 75, "valuation": 27,
    }.items()}
    original = scoring._build_factors
    scoring._build_factors = lambda *args, **kwargs: (factors, {})
    try:
        out = _build_estimated_forecast(industry, {}, {}, {}, {}, {}, {}, 50.0)
    finally:
        scoring._build_factors = original
    assert out is not None
    assert 50.0 <= out["estimated_quality"] <= 70.0
    assert out["estimated_quality"] != 45.0
