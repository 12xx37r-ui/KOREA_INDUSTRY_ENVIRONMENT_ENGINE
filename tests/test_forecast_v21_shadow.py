from kiee.scoring import _apply_long_horizon_valuation_guard, _forecast_challenger_v2


def test_provisional_valuation_mean_reversion_is_modest_and_horizon_aware():
    s6 = {"factors": {"valuation": {"score": 40.0, "quality": 24.0}}}
    s12 = {"factors": {"valuation": {"score": 40.0, "quality": 19.7}}}
    _apply_long_horizon_valuation_guard(s6, "3_6m")
    _apply_long_horizon_valuation_guard(s12, "6_12m")
    assert s6["factors"]["valuation"]["score"] == 41.5
    assert s12["factors"]["valuation"]["score"] == 42.5
    assert s12["factors"]["valuation"]["horizon_guard"]["mean_reversion_to_neutral"] > s6["factors"]["valuation"]["horizon_guard"]["mean_reversion_to_neutral"]


def test_challenger_keeps_legacy_contract_and_uses_factor_information():
    industry = {"weights_3m": {"earnings_momentum": 0.3, "demand_cycle": 0.3, "financial_conditions": 0.2, "market_internals": 0.1, "valuation": 0.1}}
    stage = {
        "score": 50.0,
        "factors": {
            "earnings_momentum": {"score": 80.0, "quality": 80.0, "available": True},
            "demand_cycle": {"score": 75.0, "quality": 80.0, "available": True},
            "financial_conditions": {"score": 65.0, "quality": 80.0, "available": True},
            "market_internals": {"score": 60.0, "quality": 80.0, "available": True},
            "valuation": {"score": 55.0, "quality": 80.0, "available": True},
        },
    }
    out = _forecast_challenger_v2(stage, "6_12m", industry)
    assert stage["score"] == 50.0
    assert out["score_v2_shadow"] > 50.0
    assert out["v2_status"] == "SHADOW_PRE_OOS"
    assert out["v2_factor_blend_weight"] <= 0.46
