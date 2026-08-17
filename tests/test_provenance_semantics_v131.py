from kiee.scoring import _build_factors


def _industry():
    return {
        'key': 'test',
        'sensitivities': {
            'rate_relief': 0.5,
            'krw_weakness': 0.4,
            'liquidity': 0.3,
            'credit_health': 0.4,
            'global_equity': 0.3,
        },
        'theme_ids': [],
    }


def _kr():
    return {
        'rate_current': 2.75, 'rate_3m': 2.8, 'rate_quality_3m': 84,
        'gov3y': 3.8, 'strength_current': 58, 'strength_3m': 69,
        'strength_quality_3m': 79, 'liquidity_current': 0.1,
        'liquidity_3m': 0.15, 'liquidity_quality_3m': 88,
        'credit_health': -0.3, 'equity_score': 60,
    }


def _gl():
    return {
        'consumer_current': 50, 'consumer_3m': 51, 'consumer_3m_quality': 70,
        'cost_pressure_current': 50, 'cost_pressure_3m': 49, 'cost_3m_quality': 70,
        'equity_current': 55, 'equity_3m': 56, 'equity_3m_quality': 70,
        'macro_current': 50, 'macro_quality': 70,
    }


def _direct(with_valuation=True):
    return {
        'market_internal_score': 60, 'market_internal_quality': 80,
        'valuation_score': 42 if with_valuation else None,
        'valuation_quality': 35 if with_valuation else 0,
        'valuation_history_ready': False,
        'valuation_history_samples': 0,
    }


def test_financial_conditions_is_model_derived_not_gap_proxy():
    policy = {'broad_proxy_deviation_shrinkage': 0.35, 'current_gap_proxy': {'enabled': False}}
    current, _ = _build_factors(_industry(), policy, _kr(), _gl(), {}, {}, _direct(), False)
    future, _ = _build_factors(_industry(), policy, _kr(), _gl(), {}, {}, _direct(), True)
    for factor in (current['financial_conditions'], future['financial_conditions']):
        assert factor['available'] is True
        assert factor['proxy'] is False
        assert factor['provenance'] == 'macro_derived'


def test_direct_krx_valuation_stays_direct_during_history_warmup():
    policy = {'broad_proxy_deviation_shrinkage': 0.35, 'current_gap_proxy': {'enabled': False}}
    current, _ = _build_factors(_industry(), policy, _kr(), _gl(), {}, {}, _direct(True), False)
    future, _ = _build_factors(_industry(), policy, _kr(), _gl(), {}, {}, _direct(True), True)
    assert current['valuation']['proxy'] is False
    assert current['valuation']['provenance'] == 'direct'
    assert future['valuation']['proxy'] is False
    assert future['valuation']['provenance'] == 'direct'


def test_missing_krx_valuation_remains_true_gap_proxy():
    policy = {'broad_proxy_deviation_shrinkage': 0.35, 'current_gap_proxy': {'enabled': False}}
    current, _ = _build_factors(_industry(), policy, _kr(), _gl(), {'components': {'valuation': {'score_normalized': 0.0, 'available': True}}}, {}, _direct(False), False)
    assert current['valuation']['proxy'] is True
    assert current['valuation']['provenance'] == 'gap_proxy'
