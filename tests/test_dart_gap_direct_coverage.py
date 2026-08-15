from pathlib import Path
import json

from kiee.dart_earnings_collector import (
    COLLECTOR_VERSION,
    MAX_FIRMS,
    TARGET_FIRMS_FOR_FULL_QUALITY,
    _cycle_direct_axes,
    _previous_rows_same_period,
    collect_industry,
)
from kiee.scoring import _apply_dart_earnings_to_factors


def test_dart_revenue_replaces_proxy_demand_without_extra_metric_source():
    factors = {
        "earnings_momentum": {"score": 48.0, "quality": 35.0, "available": True, "proxy": True},
        "demand_cycle": {"score": 45.0, "quality": 35.0, "available": True, "proxy": True},
        "pricing_margin": {"score": 47.0, "quality": 35.0, "available": True, "proxy": True},
    }
    dart = {
        "score": 62.0,
        "quality": 54.0,
        "median_yoy_pct": 30.0,
        "n_firms": 1,
        "revenue_score": 58.0,
        "revenue_quality": 54.0,
        "median_revenue_yoy_pct": 22.0,
        "revenue_n_firms": 1,
        "margin_score": 55.0,
        "margin_quality": 50.0,
        "median_margin_delta_ppt": 1.3,
        "margin_n_firms": 1,
    }
    out = _apply_dart_earnings_to_factors(factors, dart)
    assert out["earnings_momentum"]["proxy"] is False
    assert out["demand_cycle"]["proxy"] is False
    assert out["pricing_margin"]["proxy"] is False
    assert out["demand_cycle"]["dart_revenue_applied"] is True
    assert "매출액 YoY" in out["demand_cycle"]["source"]


def test_gap_collector_uses_multiple_firms_and_quality_denominator_remains_two():
    assert COLLECTOR_VERSION == "dart-earnings-v3.3-gap-multifirm-cache"
    assert MAX_FIRMS == 3
    assert TARGET_FIRMS_FOR_FULL_QUALITY == 2


def test_cycle_direct_axes_and_same_period_cache(tmp_path: Path):
    p = tmp_path / "input"
    p.mkdir()
    (p / "industry_cycle_latest.json").write_text(json.dumps({
        "industries": [
            {"industry_key": "svc", "current": {"metrics": [
                {"factor": "sales_earnings", "available": True},
            ]}},
            {"industry_key": "mfg", "current": {"metrics": [
                {"factor": "production_shipments", "available": True},
                {"factor": "price_margin", "available": True},
            ]}},
        ]
    }), encoding="utf-8")
    axes = _cycle_direct_axes(tmp_path)
    assert axes["svc"] == {"earnings_momentum", "demand_cycle"}
    assert axes["mfg"] == {"earnings_momentum", "demand_cycle", "pricing_margin"}

    prev = {
        "quarter": "HY",
        "reference_year": 2026,
        "industries": [{"industry_key": "svc", "current": {"metrics": []}}],
    }
    assert "svc" in _previous_rows_same_period(prev, "HY", 2026)
    assert _previous_rows_same_period(prev, "1Q", 2026) == {}


def test_workflow_runs_kosis_before_dart_gap_collection():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github" / "workflows" / "daily-industry-environment.yml").read_text(encoding="utf-8")
    cycle_cmd = "python -m kiee.industry_cycle_feed"
    dart_cmd = "python -m kiee.dart_earnings_collector"
    engine_cmd = "python -m kiee.cli"
    assert cycle_cmd in text and dart_cmd in text and engine_cmd in text
    assert text.index(cycle_cmd) < text.index(dart_cmd) < text.index(engine_cmd)


def test_revenue_only_filing_still_builds_direct_demand_signal(monkeypatch):
    import kiee.dart_earnings_collector as dec

    monkeypatch.setattr(dec, "_fetch_op_with_fallback", lambda *args, **kwargs: (None, None, 120.0, 100.0, 2026))
    monkeypatch.setattr(dec.time, "sleep", lambda *_: None)
    metric, detail = collect_industry(
        {"key": "svc", "krx_basket": ["000001"]},
        "dummy", {"000001": "corp"}, [0], 10, 2026, "HY", "11012",
    )
    assert metric is not None
    assert metric["score"] is None
    assert metric["revenue_score"] is not None
    assert metric["revenue_quality"] > 0
    assert metric["revenue_n_firms"] == 1
    assert "revenue=" in detail


def test_collect_industry_reuses_financial_cache_across_shared_company(monkeypatch):
    import kiee.dart_earnings_collector as dec
    calls = []
    def fake_fetch(corp_code, year, reprt_code, api_key, cache=None):
        key=(corp_code, year, reprt_code)
        if cache is not None and key in cache:
            return cache[key]
        calls.append(key)
        value=(120.0 if year == 2026 else 100.0, 1000.0 if year == 2026 else 900.0)
        if cache is not None:
            cache[key]=value
        return value
    monkeypatch.setattr(dec, "_fetch_financials", fake_fetch)
    monkeypatch.setattr(dec.time, "sleep", lambda *_: None)
    cache = {}
    count=[0]
    for key in ("cloud", "internet_platform"):
        metric, _ = collect_industry(
            {"key": key, "krx_basket": ["000001"]},
            "dummy", {"000001":"corp"}, count, 10, 2026, "HY", "11012", cache,
        )
        assert metric is not None
    assert len(calls) == 2
    assert count[0] == 2

def test_collect_industry_uses_second_firm_when_first_has_revenue_only(monkeypatch):
    import kiee.dart_earnings_collector as dec
    def fake_fallback(corp_code, *args, **kwargs):
        if corp_code == "corp1":
            return None, None, 120.0, 100.0, 2026
        return 130.0, 100.0, 1100.0, 1000.0, 2026
    monkeypatch.setattr(dec, "_fetch_op_with_fallback", fake_fallback)
    monkeypatch.setattr(dec.time, "sleep", lambda *_: None)
    metric, _ = collect_industry(
        {"key":"retail", "krx_basket":["000001","000002"]},
        "dummy", {"000001":"corp1","000002":"corp2"}, [0], 20, 2026, "HY", "11012", {},
    )
    assert metric is not None
    assert metric["score"] is not None
    assert metric["margin_score"] is not None
    assert metric["revenue_n_firms"] == 2
