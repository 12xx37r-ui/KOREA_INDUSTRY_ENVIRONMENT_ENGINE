from pathlib import Path


def test_source_contains_observed_macro_non_proxy_contract():
    text = (Path(__file__).parents[1] / 'src' / 'kiee' / 'scoring.py').read_text(encoding='utf-8')
    assert '"input_basis"] = "observed_macro_inputs"' in text
    assert '한국 금리·환율·유동성·신용 실측값 → 업종 민감도' in text
    assert '_financial_detail_text' in text


def test_valuation_history_shortage_does_not_make_direct_krx_proxy():
    text = (Path(__file__).parents[1] / 'src' / 'kiee' / 'scoring.py').read_text(encoding='utf-8')
    assert 'proxy=direct_val is None' in text
    assert 'valuation["provenance"] = "direct"' in text
    assert 'KRX 업종 대표바스켓 PER·PBR + 한국시장 상대가치' in text
