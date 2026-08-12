from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import load_all
from .krx_market import collect_sector_market
from .prospective import read_summary, update_registry
from .scoring import score_industry
from .upstream import SourceResult, UpstreamLoader
from .util import finite, read_json, roundn, utc_now_iso, write_json


def _freshness_quality(sources: dict[str, SourceResult], upstream_cfg: dict[str, Any]) -> float:
    if not sources:
        return 0.0
    source_cfg = upstream_cfg.get("sources") or {}
    scores: list[float] = []
    for name, result in sources.items():
        if not result.ok:
            scores.append(20.0)
            continue
        cfg = source_cfg.get(name) or {}
        ttl = float(cfg.get("cache_ttl_hours") or 0.0)
        max_age = float(cfg.get("max_stale_hours") or max(ttl, 1.0))
        age = result.source_age_hours
        if age is None:
            q = 70.0
        elif age <= ttl or max_age <= ttl:
            q = 100.0
        elif age <= max_age:
            q = 100.0 - 45.0 * ((age - ttl) / (max_age - ttl))
        else:
            q = 20.0
        if result.stale:
            q = min(q, 65.0)
        scores.append(max(20.0, min(100.0, q)))
    return sum(scores) / len(scores)


def _source_status(sources: dict[str, SourceResult]) -> dict[str, Any]:
    return {
        name: {
            "ok": result.ok,
            "mode": result.mode,
            "stale": result.stale,
            "age_hours": roundn(result.age_hours, 2),
            "source_generated_at": result.source_generated_at,
            "source_age_hours": roundn(result.source_age_hours, 2),
            "http_calls": result.http_calls,
            "error": result.error,
        }
        for name, result in sources.items()
    }


def _append_history(root: Path, as_of: str, results: list[dict[str, Any]]) -> None:
    path = root / "output" / "industry_environment_history.json"
    payload = read_json(path, {}) or {}
    snapshots = payload.get("snapshots") if isinstance(payload, dict) else None
    if not isinstance(snapshots, list):
        snapshots = []
    day = as_of[:10]
    snapshots = [row for row in snapshots if str(row.get("as_of", ""))[:10] != day]
    snapshots.append({
        "as_of": as_of,
        "industries": {
            row["industry_key"]: {
                "current_score": row["current"]["score"],
                "forecast_3m_score": row["forecast_3m"]["score"],
                "delta_points": row["forecast_3m"]["delta_points"],
                "quality_score": row["forecast_3m"]["quality_score"],
            }
            for row in results
        },
    })
    write_json(path, {"schema_version": "1.0.0", "snapshots": snapshots[-400:]})


def run_engine(
    root: Path,
    fixture_dir: Path | None = None,
    allow_live_krx: bool = True,
    stock_module: Any = None,
) -> dict[str, Any]:
    industries_cfg, policy, upstream_cfg = load_all(root)
    industries = list(industries_cfg["industries"])
    loader = UpstreamLoader(root, upstream_cfg, fixture_dir=fixture_dir)
    sources = loader.load_all()
    required = ["korea_rate_fx", "korea_equity", "global_bundle"]
    missing = [name for name in required if not sources.get(name) or not sources[name].ok]
    if missing:
        raise RuntimeError("required upstream unavailable/stale: " + ", ".join(missing))
    korea_rate = sources["korea_rate_fx"].payload or {}
    korea_equity = sources["korea_equity"].payload or {}
    global_bundle = sources["global_bundle"].payload or {}
    fed_source = sources.get("fed_futures")
    fed_futures = (fed_source.payload or {}) if fed_source and fed_source.ok else {}
    # The forecast collector owns the macro combination. Keep the source available
    # as an auditable input without allowing it to leak into the current industry score.
    global_bundle = dict(global_bundle)
    global_bundle["fed_futures"] = fed_futures
    boom_source = sources.get("industry_boom")
    boom = (boom_source.payload or {}) if boom_source and boom_source.ok else {}
    cycle_source = sources.get("industry_cycle")
    industry_cycle = (cycle_source.payload or {}) if cycle_source and cycle_source.ok else {}

    direct_market = collect_sector_market(
        root, industries, boom, stock_module=stock_module, allow_live=allow_live_krx,
        max_lkg_age_hours=float((policy.get("upstream_max_age_hours") or {}).get("krx_market", 120)),
        valuation_policy=policy,
    )
    freshness = _freshness_quality(sources, upstream_cfg)
    prospective_before = read_summary(root)
    as_of = utc_now_iso()
    results = [
        score_industry(industry, policy, korea_rate, korea_equity, global_bundle, boom, industry_cycle, direct_market, freshness, prospective_before)
        for industry in industries
    ]

    prospective_after = update_registry(root, results, direct_market, policy, as_of)
    # Always perform one local-only re-score after registry update so every per-industry
    # prospective_validation block is synchronized with the just-written registry summary
    # (registered/evaluated counts included). This adds zero network/API calls.
    results = [
        score_industry(industry, policy, korea_rate, korea_equity, global_bundle, boom, industry_cycle, direct_market, freshness, prospective_after)
        for industry in industries
    ]

    by_key = {row["industry_key"]: row for row in results}
    alias_lookup: dict[str, str] = {}
    for industry in industries:
        alias_lookup[industry["key"]] = industry["key"]
        alias_lookup[str(industry["label"])] = industry["key"]
        for alias in industry.get("aliases") or []:
            alias_lookup[str(alias)] = industry["key"]

    bridge = {
        "schema_version": "1.0.0",
        "engine_version": policy["engine_version"],
        "generated_at_utc": as_of,
        "by_profile_key": {
            key: {
                "industry_label": row["industry_label"],
                "current_score": row["current"]["score"],
                "current_band": row["current"]["band"],
                "forecast_3m_score": row["forecast_3m"]["score"],
                "forecast_3m_band": row["forecast_3m"]["band"],
                "delta_points": row["forecast_3m"]["delta_points"],
                "direction": row["forecast_3m"]["direction"],
                "quality_score": row["forecast_3m"]["quality_score"],
                "bounded_direction_adjustment_points": row["stock_prediction_bridge"]["bounded_direction_adjustment_points"],
                "allowed_as_auxiliary": row["stock_prediction_bridge"]["allowed_as_auxiliary"],
                "allowed_as_primary": row["stock_prediction_bridge"]["allowed_as_primary"],
            }
            for key, row in by_key.items()
        },
        "alias_to_profile_key": alias_lookup,
        "usage_rule": "기존 산업전망 current/future 값을 이 파일로 대체하되, 개별종목 주가방향에는 bounded_direction_adjustment_points만 보조 입력으로 사용합니다.",
    }

    ranked = [
        {
            "industry_key": row["industry_key"],
            "industry_label": row["industry_label"],
            "score": row.get("forecast_3m", {}).get("score"),
            "direction": row.get("forecast_3m", {}).get("direction"),
            "status": row.get("forecast_3m", {}).get("status"),
        }
        for row in results
        if finite(row.get("forecast_3m", {}).get("score")) is not None
    ]
    ranked.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)

    overall = {
        "schema_version": "1.0.0",
        "engine_version": policy["engine_version"],
        "generated_at_utc": as_of,
        "status": "ok",
        "industry_count": len(results),
        "industry_universe": {
            "classification_basis": industries_cfg.get("classification_basis") or ["GICS", "WICS", "KSIC"],
            "hierarchy_fields": ["parent_sector", "industry_group", "key"],
            "profile_count": len(industries),
            "data_gated": True,
        },
        "score_definition": "산업실물지표가 연결된 경우 현재 0~100은 생산·출하·매출·재고·가동률·고용·가격·마진·PMI/BSI를 산출하고, 전망은 신규주문·재고사이클·CAPEX·글로벌·한국 경기·실적전망·상대강도를 산업 민감도에 따라 별도 계산합니다. 50 중립.",
        "score_model": {
            "current": "industry_observed_metrics_only",
            "future": "industry_leading_metrics_plus_sensitive_macro",
            "current_excludes_macro": True,
            "future_uses_industry_sensitivities": True,
            "data_gated": True,
            "horizons": ["current", "3m", "3_6m", "6_12m"],
            "forecast_macro_sources": ["global_bundle", "korea_rate_fx", "korea_equity", "fed_futures"],
        },
        "rankings": {
            "improvement_potential_3m": ranked,
            "deterioration_risk_3m": list(reversed(ranked)),
            "status": "scored" if ranked else "pending_industry_cycle_feed",
        },
        "forecast_horizon": "향후 약 3개월",
        "industries": results,
        "source_status": _source_status(sources),
        "call_efficiency": {
            "upstream_http_calls_this_run": loader.http_calls,
            "normal_upstream_http_call_target": upstream_cfg.get("normal_upstream_http_calls_per_run", 4),
            "krx_bulk_calls_this_run": direct_market.get("normal_live_calls", 0),
            "krx_normal_target_calls": direct_market.get("normal_target_calls", 8),
            "per_company_live_calls": 0,
            "duplicate_upstream_reads_in_same_run": 0,
            "local_industry_calculation": True,
        },
        "prospective_validation": prospective_after,
        "limitations": [
            "산업별 생산·출하·재고·주문·가격·마진 원자료가 없으면 현재·전망 점수를 산출하지 않고 데이터 부족으로 표시합니다.",
            "현재경기와 향후전망은 하나의 점수로 섞지 않습니다. 전망은 3개월·3~6개월·6~12개월을 별도 슬롯으로 둡니다.",
            "산업별 EPS 컨센서스 revision 유료데이터는 사용하지 않습니다. 테마 실물·상업화 또는 한국시장 후행 EPS 대용치를 명확히 구분해 사용합니다.",
            "산업 밸류에이션은 동일 산업의 주간 PER/PBR 이력을 우선 사용합니다. 역사표본이 부족한 초기에는 전체시장 횡단면 값의 산업구조 편향을 막기 위해 중립 방향으로 강하게 축소하고 품질을 제한합니다.",
            "3개월 산업전망의 개별종목 방향예측 영향은 prospective OOS 통과 전 제한됩니다.",
        ],
    }
    output_dir = root / "output"
    write_json(output_dir / "industry_environment_latest.json", overall)
    write_json(output_dir / "industry_mapping.json", {
        "schema_version": "1.0.0", "engine_version": policy["engine_version"],
        "generated_at_utc": as_of,
        "industries": [{k: industry.get(k) for k in ("key", "label", "aliases", "theme_ids", "krx_basket", "notes", "parent_sector", "industry_group", "classification_basis", "industry_profile", "specialized_current_metrics", "specialized_leading_metrics", "current_metric_groups", "leading_metric_groups")} for industry in industries],
        "alias_to_profile_key": alias_lookup,
    })
    write_json(output_dir / "stock_prediction_bridge.json", bridge)
    # Compact static dashboard export: one JSON file for industry search/UI.
    # Browser-side loading performs no external API/GitHub calls.
    dashboard_industries = []
    for row in results:
        cfg = next((item for item in industries if item.get("key") == row.get("industry_key")), {})
        direct_row = row.get("direct_market") if isinstance(row.get("direct_market"), dict) else {}
        dashboard_industries.append({
            "industry_key": row.get("industry_key"),
            "industry_label": row.get("industry_label"),
            "aliases": row.get("aliases") or [],
            "parent_sector": cfg.get("parent_sector"),
            "industry_group": cfg.get("industry_group"),
            "current": row.get("current"),
            "forecast_3m": row.get("forecast_3m"),
            "forecast_3_6m": row.get("forecast_3_6m"),
            "forecast_6_12m": row.get("forecast_6_12m"),
            "quality": row.get("quality"),
            "score_model": row.get("score_model"),
            "companies": [
                {"ticker": code, "representative": True}
                for code in (direct_row.get("requested_basket") or cfg.get("krx_basket") or [])
            ],
        })
    write_json(output_dir / "industry_dashboard.json", {
        "schema_version": "1.0.0",
        "engine_version": policy["engine_version"],
        "generated_at_utc": as_of,
        "status": "ok",
        "industry_count": len(dashboard_industries),
        "search_rule": "industry label, key, alias, parent sector, and industry group",
        "company_rule": "configured KRX representative basket; full stock-universe linkage remains a downstream integration",
        "industries": dashboard_industries,
    })
    for row in results:
        write_json(output_dir / "industries" / f"{row['industry_key']}.json", row)
    direct_krx_available = direct_market.get("available") is True
    write_json(output_dir / "engine_health.json", {
        "status": "ok" if direct_krx_available else "degraded",
        "generated_at_utc": as_of,
        "source_status": _source_status(sources),
        "freshness_quality_score": round(freshness, 1),
        "direct_krx_available": direct_krx_available,
        "direct_krx_source_mode": direct_market.get("source_mode"),
        "direct_krx_credentials_configured": direct_market.get("krx_credentials_configured") is True,
        "direct_krx_industry_coverage_pct": direct_market.get("industry_coverage_pct", 0.0),
        "direct_krx_fresh_industry_count": direct_market.get("fresh_industry_count", 0),
        "direct_krx_lkg_reused_industry_count": direct_market.get("lkg_reused_industry_count", 0),
        "direct_krx_available_industry_count": direct_market.get("available_industry_count", 0),
        "valuation_history_ready_industry_count": direct_market.get("valuation_history_ready_industry_count", 0),
        "valuation_history_total_industry_count": direct_market.get("valuation_history_total_industry_count", len(industries)),
        "valuation_history_sampling": direct_market.get("valuation_history_sampling", "weekly-from-existing-bulk-calls"),
        "valuation_history_snapshot_count": direct_market.get("valuation_history_snapshot_count", 0),
        "valuation_history_latest_week": direct_market.get("valuation_history_latest_week"),
        "valuation_history_visible_path": direct_market.get("valuation_history_visible_path", "output/industry_valuation_history.json"),
        "valuation_history_canonical_path": direct_market.get("valuation_history_canonical_path", "output/validation/industry_valuation_history.json"),
        "direct_krx_actual_periods": direct_market.get("actual_periods") or {},
        "direct_krx_diagnostics": direct_market.get("diagnostics") or [],
        "call_efficiency": overall["call_efficiency"],
        "prospective_validation": prospective_after,
    })
    _append_history(root, as_of, results)
    return overall
