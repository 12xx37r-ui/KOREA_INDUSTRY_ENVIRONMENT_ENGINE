import json
from pathlib import Path

from kiee.krx_market import _rows


def test_paper_packaging_basket_repaired():
    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / 'config' / 'industries.json').read_text(encoding='utf-8'))
    rows = data['industries'] if isinstance(data, dict) else data
    paper = next(x for x in rows if x.get('key') == 'paper_packaging')
    assert '002310' in paper['krx_basket']
    assert '016590' in paper['krx_basket']
    assert '002010' not in paper['krx_basket']


def test_krx_rows_capture_dividend_fields():
    class FakeFrame:
        empty = False
        def iterrows(self):
            yield '088980', {'PER': 0.0, 'PBR': 0.0, 'DIV': 6.25, 'DPS': 350}
    rows = _rows(FakeFrame())
    assert rows['088980']['div'] == 6.25
    assert rows['088980']['dps'] == 350


def test_reit_dividend_fallback_is_reit_only_and_no_new_fetch():
    root = Path(__file__).resolve().parents[1]
    text = (root / 'src' / 'kiee' / 'krx_market.py').read_text(encoding='utf-8')
    assert 'reit_dividend_yield_provisional' in text
    assert 'key in {"real_estate_reit", "reit_office_logistics"}' in text
    # The fallback must use the already populated fundamental_all table, not add HTTP/API calls.
    block = text[text.index('sector_divs = ['):text.index('requested = len(basket)')]
    assert 'fundamental_all' in block
    assert '_fetch_' not in block
