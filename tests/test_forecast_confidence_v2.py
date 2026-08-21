from kiee.scoring import _forecast_confidence_v2, _apply_long_horizon_valuation_guard

def test_confidence_decays_by_horizon_pre_oos():
    stage={"quality_score":80,"data_coverage_pct":90}
    p={"prospective_min_cases":24}
    pros={"status":"PENDING","evaluated_cases":0}
    a=_forecast_confidence_v2(stage,"3m",pros,p)["forecast_confidence_v2_pct"]
    b=_forecast_confidence_v2(stage,"6_12m",pros,p)["forecast_confidence_v2_pct"]
    assert b < a

def test_long_horizon_valuation_guard_is_additive_and_mean_reverting():
    stage={"score":60,"factors":{"valuation":{"score":30.0,"quality":40.0}}}
    _apply_long_horizon_valuation_guard(stage,"6_12m")
    assert stage["score"]==60
    assert 30.0 < stage["factors"]["valuation"]["score"] < 40.0
    assert stage["factors"]["valuation"]["forecast_role"]=="historical_fair_value_mean_reversion_anchor"
