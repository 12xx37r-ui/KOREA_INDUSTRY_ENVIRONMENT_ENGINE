import tempfile, json
from pathlib import Path
from src.kiee.industry_kosis_collector import _meta_period, build_selectors, select_metric, normalize_metric, collect_with_cache

def test_meta_period():
    assert _meta_period({"PRD_DE":"2026.06"}) == "2026.06"
    assert _meta_period({"period":"2026-06"}) == "2026-06"
    assert _meta_period({}) is None

def test_selector_never_invents_code():
    assert build_selectors({"rows":[{"NM":"반도체","CODE":"001"}]}, ["반도체"])[0]["objL1"] == "001"
    assert build_selectors({"rows":[]}, ["반도체"])[0]["objL1"] == "ALL"

def test_select_metric():
    assert select_metric([{"UNIT_NM":"index","value":1}])["value"] == 1

def test_normalize():
    x = normalize_metric({"DT":"94.6","UNIT_NM":"index","PRD_DE":"2026.06"}, "utilization", "KOSIS")
    assert x["value"] == "94.6" and x["unit"] == "index"

def test_cache():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        calls = {"n":0}
        def f():
            calls["n"] += 1
            return {"ok":1}
        assert collect_with_cache("x", f, p) == {"ok":1}
        assert collect_with_cache("x", f, p) == {"ok":1}
        assert calls["n"] == 1
