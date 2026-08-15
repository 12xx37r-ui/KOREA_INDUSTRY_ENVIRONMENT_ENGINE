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


def test_gap_collector_uses_one_firm_but_quality_denominator_remains_two():
    assert COLLECTOR_VERSION == "dart-earnings-v3.2-gap-first-revenue"
    assert MAX_FIRMS == 1
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
    cycle_pos = text.find("PYTHONPATH=src python -m kiee.industry_cycle_feed")
    dart_pos = text.find("PYTHONPATH=src python -m kiee.dart_earnings_collector")
    engine_pos = text.find("PYTHONPATH=src python -m kiee.cli")
    assert cycle_pos >= 0, "workflow에 industry_cycle_feed 실행 단계가 없습니다"
    assert dart_pos >= 0, "workflow에 DART gap collector 실행 단계가 없습니다"
    assert engine_pos >= 0, "workflow에 industry engine 실행 단계가 없습니다"
    assert cycle_pos < dart_pos < engine_pos, "workflow 순서는 industry_cycle_feed -> DART gap collector -> engine 이어야 합니다"


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
