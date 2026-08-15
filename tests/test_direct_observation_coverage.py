from kiee.scoring import _apply_dart_earnings_to_factors, _direct_observed_axis_factors


def test_kosis_observed_metrics_promote_public_axes_without_proxy():
    stage = {
        "metrics": [
            {"factor": "production_shipments", "score": 72.0, "quality": 85.0, "available": True},
            {"factor": "utilization", "score": 64.0, "quality": 85.0, "available": True},
            {"factor": "employment", "score": 58.0, "quality": 75.0, "available": True},
        ]
    }
    factors = _direct_observed_axis_factors(stage)
    assert factors["earnings_momentum"]["available"] is True
    assert factors["earnings_momentum"]["proxy"] is False
    assert factors["demand_cycle"]["available"] is True
    assert factors["demand_cycle"]["proxy"] is False
    assert "pricing_margin" not in factors


def test_dart_direct_replaces_proxy_and_adds_margin_axis():
    factors = {
        "earnings_momentum": {"score": 40.0, "quality": 35.0, "available": True, "proxy": True, "source": "proxy"},
        "pricing_margin": {"score": 45.0, "quality": 35.0, "available": True, "proxy": True, "source": "proxy"},
    }
    dart = {
        "score": 68.0,
        "quality": 70.0,
        "median_yoy_pct": 36.0,
        "n_firms": 2,
        "margin_score": 61.0,
        "margin_quality": 68.0,
        "median_margin_delta_ppt": 2.93,
        "margin_n_firms": 2,
    }
    out = _apply_dart_earnings_to_factors(factors, dart)
    assert out["earnings_momentum"]["proxy"] is False
    assert out["earnings_momentum"]["score"] == 68.0
    assert out["pricing_margin"]["proxy"] is False
    assert out["pricing_margin"]["score"] == 61.0


def test_service_production_is_direct_for_earnings_and_demand():
    stage = {
        "metrics": [
            {"factor": "sales_earnings", "score": 66.0, "quality": 85.0, "available": True},
        ]
    }
    factors = _direct_observed_axis_factors(stage)
    assert factors["earnings_momentum"]["proxy"] is False
    assert factors["demand_cycle"]["proxy"] is False
    assert factors["earnings_momentum"]["score"] == 66.0
    assert factors["demand_cycle"]["score"] == 66.0
