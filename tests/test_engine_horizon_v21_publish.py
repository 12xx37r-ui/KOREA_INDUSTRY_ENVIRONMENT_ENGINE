from kiee import engine


def _fake_block(score=46.0):
    return {
        "score": score,
        "quality_score": 56.0,
        "data_coverage_pct": 100.0,
        "factors": {
            "earnings_momentum": {"score": 52.0, "quality": 50.0, "available": True},
            "demand_cycle": {"score": 48.0, "quality": 55.0, "available": True},
            "pricing_margin": {"score": 50.0, "quality": 70.0, "available": True},
            "financial_conditions": {"score": 36.0, "quality": 80.0, "available": True},
            "market_internals": {"score": 44.0, "quality": 60.0, "available": True},
            "valuation": {"score": 43.0, "quality": 24.0, "available": True},
        },
    }


def test_independent_horizon_blocks_are_redecorated_after_replacement(monkeypatch):
    # Regression test for engine.py replacing the 6m/12m stages after scoring.py
    # already added V2 metadata. The final published blocks must retain it.
    policy = {"prospective_min_cases": 24}
    prospective = {"status": "PENDING", "evaluated_cases": 0}
    industry = {"weights_3m": {
        "earnings_momentum": .2, "demand_cycle": .3, "pricing_margin": .15,
        "financial_conditions": .15, "market_internals": .1, "valuation": .1,
    }}

    for horizon, block in (("3_6m", _fake_block()), ("6_12m", _fake_block())):
        legacy_val = block["factors"]["valuation"]["score"]
        valuation_shadow = {"factors": {"valuation": dict(block["factors"]["valuation"])}}
        engine._apply_long_horizon_valuation_guard(valuation_shadow, horizon)
        guarded = valuation_shadow["factors"]["valuation"]
        block["factors"]["valuation"]["score_v2_shadow"] = guarded["score"]
        block.update(engine._forecast_confidence_v2(block, horizon, prospective, policy))
        block.update(engine._forecast_challenger_v2(block, horizon, industry))

        assert block["factors"]["valuation"]["score"] == legacy_val
        assert block["factors"]["valuation"]["score_v2_shadow"] > legacy_val
        assert block["forecast_confidence_v2_pct"] > 0
        assert block["score_v2_shadow"] is not None
        assert block["v2_status"] == "SHADOW_PRE_OOS"
