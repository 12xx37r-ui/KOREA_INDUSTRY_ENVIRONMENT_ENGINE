from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import load_all
from .krx_market import collect_sector_market
from .prospective import read_summary, update_registry
from .scoring import score_industry, _aggregate_score, _score_band, _delta_strength
from .upstream import SourceResult, UpstreamLoader
from .util import clamp, finite, find_month_row, nested, read_json, roundn, utc_now_iso, write_json
from .api_health import add_network_calls, record_cache, record_lkg, record_fallback, record_unavailable, record_state, flush as flush_api_health



def _quality_from_gate(gate: Any, default: float = 55.0) -> float:
    if not isinstance(gate, dict):
        return default
    if gate.get("passed") is True:
        return 88.0
    if gate.get("performance_candidate") is True or gate.get("candidate") is True:
        return 72.0
    return default


def _horizon_forecast_value(card: dict[str, Any], months: int, fallback: float | None = None) -> tuple[float | None, float]:
    row = (card.get("forecasts") or {}).get(f"{months}m") if isinstance(card, dict) else None
    if not isinstance(row, dict):
        return fallback, 35.0 if fallback is not None else 0.0
    value = finite(row.get("forecast"), fallback)
    q = _quality_from_gate(row.get("quality_gate") or {}, 55.0)
    return value, q


def _korea_horizon_inputs(korea_rate: dict[str, Any], korea_equity: dict[str, Any], months: int) -> dict[str, Any]:
    current_rate = finite(nested(korea_rate, "rate", "current_rate_pct"))
    rate = finite(nested(korea_rate, "rate", "calendar_horizon_estimates", f"{months}m"), current_rate)
    rate_q = finite(nested(korea_rate, "rate", "quality_gate", "forecast_quality_score"), 70.0) or 70.0
    fx_current = finite(nested(korea_rate, "fx", "current_usdkrw"))
    fx_row = find_month_row(nested(korea_rate, "fx", "forecast_path", default=[]), months)
    fx = finite(fx_row.get("point_forecast"), fx_current)
    fx_q = finite(fx_row.get("model_quality_score"), 60.0) or 60.0
    liq_current = finite(nested(korea_rate, "krw_liquidity", "current", "liquidity_score"), 0.0) or 0.0
    liq_row = find_month_row(nested(korea_rate, "krw_liquidity", "forecast_path", default=[]), months)
    liq = finite(liq_row.get("liquidity_score"), liq_current) or liq_current
    liq_q = finite(liq_row.get("forecast_quality_score"), 70.0) or 70.0
    strength_current = finite(nested(korea_rate, "krw_strength", "current", "strength_score"), 50.0) or 50.0
    strength_row = find_month_row(nested(korea_rate, "krw_strength", "forecast_path", default=[]), months)
    strength = finite(strength_row.get("strength_score"), strength_current) or strength_current
    strength_q = finite(strength_row.get("independent_oos_quality_score"), strength_row.get("model_quality_score")) or 60.0
    credit = finite(nested(korea_equity, "components", "credit_spread", "score_normalized"), 0.0) or 0.0
    gov3y = finite(nested(korea_equity, "current_inputs", "credit", "gov_3y_pct"))
    return {
        "rate_current": current_rate, "rate": rate, "rate_q": clamp(rate_q,0,100),
        "fx_current": fx_current, "fx": fx, "fx_q": clamp(fx_q,0,100),
        "liquidity": clamp(liq,-1,1), "liquidity_q": clamp(liq_q,0,100),
        "strength": clamp(strength,0,100), "strength_q": clamp(strength_q,0,100),
        "credit": clamp(credit,-1,1), "gov3y": gov3y,
    }


def _financial_horizon_factor(industry: dict[str, Any], ki: dict[str, Any]) -> dict[str, Any]:
    sens = industry.get("sensitivities") or {}
    rate_level_parts=[]
    if ki.get("rate_current") is not None:
        rate_level_parts.append(clamp((3.25-float(ki["rate_current"]))/1.5,-1,1))
    if ki.get("gov3y") is not None:
        rate_level_parts.append(clamp((3.75-float(ki["gov3y"]))/1.5,-1,1))
    rate_level=sum(rate_level_parts)/len(rate_level_parts) if rate_level_parts else 0.0
    rate_change=0.0
    if ki.get("rate_current") is not None and ki.get("rate") is not None:
        rate_change=clamp((float(ki["rate_current"])-float(ki["rate"]))/0.75,-1,1)
    rate_impulse=0.35*rate_level+0.65*rate_change
    weakness=clamp((50.0-float(ki.get("strength",50.0)))/50.0,-1,1)
    pieces=[
        (rate_impulse, float(sens.get("rate_relief",0.0)), ki.get("rate_q",60.0)),
        (weakness, float(sens.get("krw_weakness",0.0)), ki.get("strength_q",60.0)),
        (float(ki.get("liquidity",0.0)), float(sens.get("liquidity",0.0)), ki.get("liquidity_q",60.0)),
        (float(ki.get("credit",0.0)), float(sens.get("credit_health",0.0)), 60.0),
    ]
    den=sum(abs(w) for _,w,_ in pieces if abs(w)>0)
    if den<=0:
        return {"score":50.0,"quality":35.0,"available":True,"proxy":False,"provenance":"macro_derived"}
    impulse=sum(v*w for v,w,_ in pieces)/den
    q=sum(abs(w)*q for _,w,q in pieces)/den
    return {"score":round(clamp(50+50*impulse,0,100),2),"quality":round(clamp(q,0,100),1),"available":True,"proxy":False,"provenance":"macro_derived","input_basis":"horizon_specific_observed_forecasts"}


def _build_independent_horizon_block(row: dict[str, Any], industry: dict[str, Any], policy: dict[str, Any], korea_rate: dict[str, Any], korea_equity: dict[str, Any], global_bundle: dict[str, Any], months: int, current_score: float) -> dict[str, Any]:
    base = row.get("forecast_3m") or {}
    base_factors = base.get("factors") or {}
    cards = global_bundle.get("cards") or {}
    c9 = cards.get("9") or cards.get(9) or {}
    c10 = cards.get("10") or cards.get(10) or {}
    consumer, consumer_q = _horizon_forecast_value(c9, months, finite(c9.get("current"),50.0))
    cost, cost_q = _horizon_forecast_value(c10, months, finite(c10.get("current"),50.0))
    sens = industry.get("sensitivities") or {}
    consumer_sens=clamp(abs(float(sens.get("consumer_cycle",0.0))),0,1)
    cost_sens=float(sens.get("cost_relief",0.0))
    demand_macro=clamp(50+(float(consumer or 50)-50)*max(0.35,consumer_sens),0,100)
    pricing_macro=clamp(50-(float(cost or 50)-50)*cost_sens,0,100)
    ki=_korea_horizon_inputs(korea_rate,korea_equity,months)
    financial=_financial_horizon_factor(industry,ki)

    def bf(name, default=50.0):
        f=base_factors.get(name) or {}
        return finite(f.get("score"),default) or default, finite(f.get("quality"),40.0) or 40.0
    e3,eq=bf("earnings_momentum"); d3,dq=bf("demand_cycle"); p3,pq=bf("pricing_margin"); m3,mq=bf("market_internals"); v3,vq=bf("valuation")
    # Horizon-specific macro paths move the medium/long blocks independently; 3m
    # factor values remain industry anchors rather than being copied wholesale.
    macro_weight = 0.45 if months==6 else 0.62
    factors={}
    factors["earnings_momentum"]={"score":round(clamp(e3*(1-macro_weight*0.45)+demand_macro*(macro_weight*0.45),0,100),2),"quality":round(min(eq,consumer_q)*0.9,1),"available":True,"proxy":False,"source":f"산업 3개월 실적선행 앵커 + 글로벌 수요 {months}개월 경로"}
    factors["demand_cycle"]={"score":round(clamp(d3*(1-macro_weight)+demand_macro*macro_weight,0,100),2),"quality":round(min(dq,consumer_q),1),"available":True,"proxy":False,"source":f"산업 수요선행 + 글로벌 고용·소비 {months}개월 독립예측"}
    factors["pricing_margin"]={"score":round(clamp(p3*(1-macro_weight)+pricing_macro*macro_weight,0,100),2),"quality":round(min(pq,cost_q),1),"available":True,"proxy":False,"source":f"산업 마진선행 + 글로벌 원가압력 {months}개월 독립예측"}
    factors["financial_conditions"]={**financial,"source":f"한국 금리·환율·유동성·원화강도 {months}개월 경로 → 업종 민감도"}
    # Market internals lose persistence with horizon; combine them with horizon demand/financial conditions.
    market_keep=0.40 if months==6 else 0.20
    structural=(demand_macro+float(financial["score"]))/2
    factors["market_internals"]={"score":round(clamp(m3*market_keep+structural*(1-market_keep),0,100),2),"quality":round(min(mq*market_keep+min(consumer_q,float(financial["quality"]))*(1-market_keep),80.0),1),"available":True,"proxy":True,"source":f"KRX 단기 내부환경 감쇠 + {months}개월 수요·금융 구조환경"}
    # Valuation is a slow-moving direct anchor; keep score but decay freshness/quality by horizon.
    factors["valuation"]={"score":round(v3,2),"quality":round(vq*(0.88 if months==6 else 0.72),1),"available":True,"proxy":bool((base_factors.get("valuation") or {}).get("proxy")),"provenance":(base_factors.get("valuation") or {}).get("provenance","direct"),"source":f"현재 KRX 업종 밸류에이션 장기 평균회귀 앵커 ({months}개월)"}

    base_w=industry.get("weights_3m") or {}
    mult={6:{"earnings_momentum":1.00,"demand_cycle":1.08,"pricing_margin":1.10,"financial_conditions":1.35,"market_internals":0.55,"valuation":1.45},12:{"earnings_momentum":0.85,"demand_cycle":1.00,"pricing_margin":1.20,"financial_conditions":1.55,"market_internals":0.30,"valuation":1.75}}[months]
    raww={k:max(0.0,float(base_w.get(k,0.0))*mult[k]) for k in mult}; total=sum(raww.values()) or 1.0
    weights={k:v/total for k,v in raww.items()}
    agg=_aggregate_score(factors,weights)
    score=float(agg.get("score") or 50.0)
    delta=round(score-current_score,1)
    qcov=float(agg.get("quality_weighted_coverage_pct") or 0.0)
    return {
        "score":round(score,1),"band":_score_band(score,policy),"delta_points":delta,
        "direction":"개선" if delta>1 else ("악화" if delta<-1 else "유지"),
        "change_strength":_delta_strength(delta,policy),"quality_score":round(min(qcov,80.0 if months==6 else 72.0),1),
        "data_coverage_pct":round(float(agg.get("base_data_coverage_pct") or 0.0),1),
        "quality_weighted_coverage_pct":round(qcov,1),"factors":factors,"contributions":agg.get("contributions") or [],
        "metrics":[],"positive_indicators":[],"negative_indicators":[],"status":"estimated","score_source":"independent_horizon_model",
        "estimated_score":round(score,1),"estimated_quality":round(min(qcov,80.0 if months==6 else 72.0),1),
        "available_factor_count":sum(1 for f in factors.values() if f.get("available")),
        "horizon_basis":f"independent_{months}m_multi_source_model","horizon_specific_inputs":True,
        "horizon_inputs":{
            "global_consumer_forecast":roundn(consumer,2),"global_consumer_quality":roundn(consumer_q,1),
            "global_cost_pressure_forecast":roundn(cost,2),"global_cost_quality":roundn(cost_q,1),
            "korea_policy_rate_forecast_pct":roundn(ki.get("rate"),3),"usdkrw_forecast":roundn(ki.get("fx"),2),
            "krw_liquidity_forecast":roundn(ki.get("liquidity"),4),"krw_strength_forecast":roundn(ki.get("strength"),2),
        },
        "model_note":"기존 원천의 6/12개월 독립 전망값을 재사용하며 새 외부 API 호출은 추가하지 않습니다.",
    }


def _reconcile_six_axis_outputs(results: list[dict[str, Any]], industries: list[dict[str, Any]], policy: dict[str, Any], korea_rate: dict[str, Any] | None = None, korea_equity: dict[str, Any] | None = None, global_bundle: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Make the published current headline consistent with its six displayed axes.

    The cycle feed may have only one observed KOSIS metric while DART, KRX and
    observed macro inputs already populate the remaining public axes.  In that
    case the old headline kept the single-metric feed score/coverage even though
    the factor table and contribution table were based on a much broader six-axis
    information set.  Reconcile only when at least two usable axes exist and the
    six-axis coverage is genuinely broader than the observed-feed coverage.

    Backward compatibility is preserved: ``score_source`` remains unchanged and
    the original observed anchor score/coverage stay in ``observed_*`` fields.
    No new external call is made here; this is pure local post-processing of the
    already-scored result.
    """
    by_key = {str(ind.get("key")): ind for ind in industries}
    for row in results:
        current = row.get("current")
        if not isinstance(current, dict) or current.get("status") != "scored":
            continue
        factors = current.get("factors")
        if not isinstance(factors, dict):
            continue
        industry = by_key.get(str(row.get("industry_key"))) or {}
        weights = industry.get("weights_current") or {}
        if not weights:
            continue
        usable_axes = sum(
            1
            for key, factor in factors.items()
            if isinstance(factor, dict)
            and finite(factor.get("score")) is not None
            and (finite(factor.get("quality"), 0.0) or 0.0) > 0
            and float(weights.get(key, 0.0) or 0.0) > 0
        )
        if usable_axes < 2:
            continue

        agg = _aggregate_score(factors, weights)
        six_axis_coverage = float(agg.get("base_data_coverage_pct") or 0.0)
        qcoverage = float(agg.get("quality_weighted_coverage_pct") or 0.0)
        observed_coverage = finite(current.get("observed_coverage_pct"), current.get("data_coverage_pct")) or 0.0
        if six_axis_coverage <= observed_coverage + 0.5:
            continue

        legacy_score = finite(current.get("score"))
        legacy_quality = finite(current.get("quality_score"))
        final_score = float(agg.get("score") or 50.0)
        current["legacy_observed_anchor_score"] = roundn(legacy_score, 1)
        current["legacy_observed_anchor_quality"] = roundn(legacy_quality, 1)
        current["score"] = round(final_score, 1)
        current["band"] = _score_band(final_score, policy)
        current["data_coverage_pct"] = round(six_axis_coverage, 1)
        current["quality_weighted_coverage_pct"] = round(qcoverage, 1)
        current["quality_score"] = round(min(95.0, qcoverage), 1)
        current["score_basis"] = "six_axis_quality_weighted_aggregate"
        current["six_axis_available_factor_count"] = usable_axes
        current["six_axis_raw_score"] = round(float(agg.get("raw_score") or final_score), 1)
        current["notes"] = (
            f"관측 산업피드 coverage {observed_coverage:.1f}%는 원자료 anchor 범위입니다. "
            f"최종 현재점수는 DART·KOSIS·KRX·실측 금융환경을 포함한 {usable_axes}개 6축의 "
            f"품질가중 집계로 산출하며, 6축 coverage {six_axis_coverage:.1f}% / "
            f"품질가중 coverage {qcoverage:.1f}%를 사용합니다."
        )

        quality = row.get("quality")
        if isinstance(quality, dict):
            quality["observed_metric_coverage_pct"] = round(observed_coverage, 1)
            quality["current_six_axis_coverage_pct"] = round(six_axis_coverage, 1)
            quality["current_six_axis_quality_weighted_coverage_pct"] = round(qcoverage, 1)

        model = row.get("model")
        if isinstance(model, dict):
            model["current"] = "industry_six_axis_with_observed_anchor"

        interpretation = row.get("interpretation")
        if isinstance(interpretation, dict):
            interpretation["headline"] = f"현재 {current.get('band', '중립')} {final_score:.0f}/100"
            interpretation["beginner"] = (
                "현재점수는 산업 실물 관측치만 단독 사용하지 않고, 실제로 화면에 표시되는 "
                "산업실적·수요·가격/마진·금융환경·KRX 내부환경·밸류에이션 6축을 "
                "자료품질로 가중해 합산합니다. 관측 산업피드 점수는 별도 anchor로 보존합니다."
            )

        # Current score changed, so all published forecast deltas must be reconciled
        # against the same current baseline.  Forecast scores themselves are untouched.
        for block_name in ("forecast_3m", "forecast_3_6m", "forecast_6_12m"):
            block = row.get(block_name)
            if not isinstance(block, dict):
                continue
            future_score = finite(block.get("score"))
            if future_score is None:
                continue
            delta = round(float(future_score) - final_score, 1)
            block["delta_points"] = delta
            block["direction"] = "개선" if delta > 1 else ("악화" if delta < -1 else "유지")
            block["change_strength"] = _delta_strength(delta, policy)
            if block_name == "forecast_3m":
                block["horizon_basis"] = "3m_model"
                block["horizon_specific_inputs"] = True

        # Replace extrapolated medium/long horizons with true horizon-specific
        # models using existing 6m/12m upstream forecasts. No new network calls.
        if korea_rate is not None and korea_equity is not None and global_bundle is not None:
            row["forecast_3_6m"] = _build_independent_horizon_block(
                row, industry, policy, korea_rate, korea_equity, global_bundle, 6, final_score
            )
            row["forecast_6_12m"] = _build_independent_horizon_block(
                row, industry, policy, korea_rate, korea_equity, global_bundle, 12, final_score
            )
    return results

def _company_name_map(root: Path) -> dict[str, str]:
    """Reuse the DART corpCode cache; no extra network call is made here."""
    cached = read_json(root / "input_cache" / "dart_corpcode_map.json", {}) or {}
    names = cached.get("names") if isinstance(cached, dict) else {}
    return {str(k).zfill(6): str(v) for k, v in (names or {}).items() if str(k) and str(v)}


def _company_rows(industry: dict[str, Any], names: dict[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in industry.get("krx_basket") or []:
        ticker = str(raw).zfill(6)
        raw_name = str(names.get(ticker) or "").strip()
        name = raw_name if raw_name and raw_name != ticker and not raw_name.isdigit() else ""
        rows.append({
            "ticker": ticker,
            "name": name,
            "market": "KR",
            "representative": True,
        })
    return rows


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


def _source_status(sources: dict[str, SourceResult], upstream_cfg: dict[str, Any]) -> dict[str, Any]:
    """Expose cache/content freshness without changing any scoring decision.

    `stale` remains the authoritative loader flag.  The extra fields make it clear
    when a source is old but still inside its configured max_stale_hours window.
    """
    source_cfg = upstream_cfg.get("sources") or {}
    rows: dict[str, Any] = {}
    for name, result in sources.items():
        cfg = source_cfg.get(name) or {}
        ttl = float(cfg.get("cache_ttl_hours") or 0.0)
        max_age = float(cfg.get("max_stale_hours") or 0.0)
        age = result.source_age_hours
        ratio = (float(age) / max_age) if age is not None and max_age > 0 else None
        if result.stale or not result.ok:
            freshness_state = "stale" if result.stale else "unavailable"
        elif age is None:
            freshness_state = "unknown"
        elif ttl > 0 and age <= ttl:
            freshness_state = "fresh"
        elif max_age > 0 and age >= max_age * 0.90:
            freshness_state = "near_stale"
        else:
            freshness_state = "aging"
        rows[name] = {
            "ok": result.ok,
            "mode": result.mode,
            "stale": result.stale,
            "age_hours": roundn(result.age_hours, 2),
            "source_generated_at": result.source_generated_at,
            "source_age_hours": roundn(result.source_age_hours, 2),
            "cache_ttl_hours": ttl,
            "stale_after_hours": max_age,
            "source_age_ratio_to_stale": roundn(ratio, 3),
            "freshness_state": freshness_state,
            "http_calls": result.http_calls,
            "error": result.error,
        }
    return rows


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
                "current_score": row["current"].get("score"),
                "forecast_3m_score": row["forecast_3m"].get("score"),
                "delta_points": row["forecast_3m"].get("delta_points"),
                "quality_score": row["forecast_3m"].get("quality_score"),
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
    # 관세청 nowcasting 데이터 로드 (선택적 — 없으면 None으로 유지)
    nowcast_raw_path = root / "input" / "customs_nowcast_raw.json"
    nowcast_data = read_json(nowcast_raw_path) if nowcast_raw_path.exists() else None
    # DART 분기 실적 데이터 로드 (선택적)
    dart_raw_path = root / "input" / "dart_earnings_raw.json"
    dart_data = read_json(dart_raw_path) if dart_raw_path.exists() else None

    direct_market = collect_sector_market(
        root, industries, boom, stock_module=stock_module, allow_live=allow_live_krx,
        max_lkg_age_hours=float((policy.get("upstream_max_age_hours") or {}).get("krx_market", 120)),
        valuation_policy=policy,
    )
    # pykrx/GitHub use their own HTTP stacks, so register their actual call
    # counts in the common workflow health ledger without changing collectors.
    add_network_calls("GITHUB", int(loader.http_calls or 0))
    add_network_calls("KRX", int(direct_market.get("normal_live_calls", 0) or 0))
    if direct_market.get("available") is True:
        record_state("KRX", "LIVE")
    if int(direct_market.get("lkg_reused_industry_count", 0) or 0) > 0:
        record_lkg("KRX")
    if direct_market.get("available") is not True:
        record_unavailable("KRX")
    if int(loader.http_calls or 0) > 0:
        record_state("GITHUB", "LIVE")
    for _src in sources.values():
        _mode = str(getattr(_src, "mode", "") or "").lower()
        if "cache-not-modified" in _mode:
            record_cache("GITHUB")
        if "fallback" in _mode:
            record_fallback("GITHUB")
        if "stale" in _mode or "last-known-good" in _mode:
            record_lkg("GITHUB")
    freshness = _freshness_quality(sources, upstream_cfg)
    prospective_before = read_summary(root)
    as_of = utc_now_iso()
    results = [
        score_industry(industry, policy, korea_rate, korea_equity, global_bundle, boom, industry_cycle, direct_market, freshness, prospective_before, nowcast_data=nowcast_data, dart_data=dart_data)
        for industry in industries
    ]
    results = _reconcile_six_axis_outputs(results, industries, policy, korea_rate, korea_equity, global_bundle)

    prospective_after = update_registry(root, results, direct_market, policy, as_of)
    results = [
        score_industry(industry, policy, korea_rate, korea_equity, global_bundle, boom, industry_cycle, direct_market, freshness, prospective_after, nowcast_data=nowcast_data, dart_data=dart_data)
        for industry in industries
    ]
    results = _reconcile_six_axis_outputs(results, industries, policy, korea_rate, korea_equity, global_bundle)

    # 대표기업은 기존 KRX basket을 그대로 노출한다. 기업명은 이미 DART
    # corpCode ZIP을 받은 날에 저장된 로컬 캐시를 재사용하므로 추가 호출 0회.
    _names = _company_name_map(root)
    _cfg_by_key = {str(ind.get("key")): ind for ind in industries}
    for _row in results:
        _cfg = _cfg_by_key.get(str(_row.get("industry_key"))) or {}
        _row["companies"] = _company_rows(_cfg, _names)

    # ── OOS 상태 요약을 engine_health에도 반영 ───────────────────────────────
    oos_status = str(prospective_after.get("status", "PENDING"))
    oos_cases = int(prospective_after.get("evaluated_cases", 0))
    from .scoring import _oos_bridge_limits
    oos_limits = _oos_bridge_limits(oos_status, oos_cases, policy)
    oos_health_note = f"OOS {oos_status} ({oos_cases}건 평가): 현재 bridge 허용한도 ±{oos_limits['max_points']}pt"

    # 출력 계약 강제 정규화: score=None이면 quality_score도 반드시 0.0
    # (scoring.py가 score를 None으로 내리면서 quality를 남겨두는 경우 방어)
    _SCORE_BLOCKS = ("current", "forecast_3m", "forecast_3_6m", "forecast_6_12m")
    for _row in results:
        for _block in _SCORE_BLOCKS:
            _b = _row.get(_block)
            if isinstance(_b, dict) and _b.get("score") is None:
                _b["quality_score"] = 0.0
                _b["quality_weighted_coverage_pct"] = 0.0

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
                "current_score": row["current"].get("score"),
                "current_band": row["current"].get("band", "데이터 부족"),
                "forecast_3m_score": row["forecast_3m"].get("score"),
                "forecast_3m_band": row["forecast_3m"].get("band", "데이터 부족"),
                "delta_points": row["forecast_3m"].get("delta_points"),
                "direction": row["forecast_3m"].get("direction"),
                "quality_score": row["forecast_3m"].get("quality_score"),
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
        "source_status": _source_status(sources, upstream_cfg),
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
    for row in results:
        write_json(output_dir / "industries" / f"{row['industry_key']}.json", row)
    direct_krx_available = direct_market.get("available") is True
    # ── industry_dashboard.json ──────────────────────────────────────────────
    # GAS/클라이언트가 기업 검색 후 산업경기를 표시할 때 직접 소비하는
    # 컴팩트 뷰. URL·네트워크 설정을 포함하지 않는다(테스트 계약 포함).
    _industry_by_key = {ind["key"]: ind for ind in industries}
    dashboard_rows = []
    for row in results:
        key = row["industry_key"]
        cfg = _industry_by_key.get(key, {})
        basket = cfg.get("krx_basket") or []
        dashboard_rows.append({
            "industry_key": key,
            "industry_label": row.get("industry_label", ""),
            "aliases": row.get("aliases") or [],
            "parent_sector": cfg.get("parent_sector"),
            "industry_group": cfg.get("industry_group"),
            "current": {k: row["current"].get(k) for k in ("score", "band", "quality_score", "data_coverage_pct", "quality_weighted_coverage_pct", "factors", "contributions", "status", "reason")},
            "forecast_3m": {k: row["forecast_3m"].get(k) for k in ("score", "band", "quality_score", "data_coverage_pct", "quality_weighted_coverage_pct", "factors", "contributions", "status", "reason", "delta_points", "change_strength", "direction", "top_positive_reasons", "top_negative_reasons")},
            "forecast_3_6m": {k: row["forecast_3_6m"].get(k) for k in ("score", "band", "quality_score", "status")},
            "forecast_6_12m": {k: row["forecast_6_12m"].get(k) for k in ("score", "band", "quality_score", "status")},
            "quality": row.get("quality"),
            "score_model": row.get("score_model"),
            "companies": row.get("companies") or [{"ticker": str(t).zfill(6), "name": "", "market": "KR", "representative": True} for t in basket],
        })
    write_json(output_dir / "industry_dashboard.json", {
        "schema_version": "1.0.0",
        "engine_version": policy["engine_version"],
        "generated_at_utc": as_of,
        "status": overall["status"] if isinstance(overall["status"], str) else overall["status"].get("status", "ok"),
        "industry_count": len(dashboard_rows),
        "search_rule": "industry label, key, alias, parent sector, and industry group",
        "company_rule": "configured KRX representative basket; company names reuse cached DART corpCode metadata when available; no per-company live calls",
        "industries": dashboard_rows,
    })
    # ────────────────────────────────────────────────────────────────────────
    # 현재 6축 출처를 direct / macro_derived / gap_proxy / structural_pending로 분리한다.
    # financial_conditions는 설계상 거시환경 파생축이다.
    # REIT 2개 valuation은 일반기업 PER/PBR 직접값을 억지로 만들지 않고 구조적 대기로
    # 분리한다. 계산값 자체는 변경하지 않고 health 분류만 정리한다.
    _current_factor_rows = []
    _current_factor_rows_with_axis = []
    _core_direct_industries = 0
    _core_keys = ("earnings_momentum", "demand_cycle", "pricing_margin")
    _structural_pending_axes = {
        ("real_estate_reit", "valuation"),
        ("reit_office_logistics", "valuation"),
    }
    _gap_by_factor = {k: [] for k in ("earnings_momentum", "demand_cycle", "pricing_margin", "valuation")}
    _structural_pending_by_factor = {"valuation": []}
    _core_gap_industry_keys: list[str] = []

    for _row in results:
        _industry_key = str(_row.get("industry_key") or "")
        _industry_label = str(_row.get("industry_label") or _row.get("label") or _industry_key)
        _factors = (_row.get("current") or {}).get("factors") or {}
        _core_direct = True
        for _axis, _factor in _factors.items():
            if isinstance(_factor, dict) and _factor.get("available"):
                _current_factor_rows.append(_factor)
                _current_factor_rows_with_axis.append((_industry_key, _industry_label, _axis, _factor))
            if _axis in _gap_by_factor and isinstance(_factor, dict) and _factor.get("available") and _factor.get("proxy"):
                entry = {"industry_key": _industry_key, "industry_label": _industry_label}
                if (_industry_key, _axis) in _structural_pending_axes:
                    _structural_pending_by_factor.setdefault(_axis, []).append(entry)
                else:
                    _gap_by_factor[_axis].append(entry)
        for _k in _core_keys:
            _f = _factors.get(_k) or {}
            if not (isinstance(_f, dict) and _f.get("available") and not _f.get("proxy")):
                _core_direct = False
        if _core_direct:
            _core_direct_industries += 1
        else:
            _core_gap_industry_keys.append(_industry_key)

    def _factor_provenance(_industry_key: str, _axis: str, _factor: dict[str, Any]) -> str:
        if _factor.get("provenance") == "macro_derived" or _axis == "financial_conditions":
            return "macro_derived"
        if _factor.get("proxy"):
            if (_industry_key, _axis) in _structural_pending_axes:
                return "structural_pending"
            return "gap_proxy"
        return "direct"

    _provenance_counts = {"direct": 0, "macro_derived": 0, "gap_proxy": 0, "structural_pending": 0}
    for _industry_key, _industry_label, _axis, _factor in _current_factor_rows_with_axis:
        _provenance_counts[_factor_provenance(_industry_key, _axis, _factor)] += 1
    _proxy_current_factor_count = sum(1 for _f in _current_factor_rows if _f.get("proxy"))
    _direct_current_factor_count = len(_current_factor_rows) - _proxy_current_factor_count
    _gap_proxy_denominator = _provenance_counts["direct"] + _provenance_counts["gap_proxy"]
    write_json(output_dir / "engine_health.json", {
        "status": "ok" if direct_krx_available else "degraded",
        "generated_at_utc": as_of,
        "source_status": _source_status(sources, upstream_cfg),
        "freshness_quality_score": round(freshness, 1),
        "current_factor_available_count": len(_current_factor_rows),
        "current_factor_direct_count": _direct_current_factor_count,
        "current_factor_proxy_count": _proxy_current_factor_count,
        "current_factor_proxy_pct": round((_proxy_current_factor_count / len(_current_factor_rows) * 100.0), 1) if _current_factor_rows else 0.0,
        "current_factor_provenance_counts": _provenance_counts,
        "current_factor_macro_derived_count": _provenance_counts["macro_derived"],
        "current_factor_gap_proxy_count": _provenance_counts["gap_proxy"],
        "current_factor_gap_proxy_pct_ex_macro": round((_provenance_counts["gap_proxy"] / _gap_proxy_denominator * 100.0), 1) if _gap_proxy_denominator else 0.0,
        "current_factor_structural_pending_count": _provenance_counts["structural_pending"],
        "current_gap_proxy_by_factor": _gap_by_factor,
        "current_structural_pending_by_factor": _structural_pending_by_factor,
        "structural_pending_note": "REIT valuation 2개는 일반 산업 데이터 gap이 아니라 REIT 전용 valuation 직접소스 대기입니다. 기존 점수 산식은 변경하지 않습니다.",
        "current_direct_by_factor": {
            _axis: sum(1 for _row in results if ((_row.get("current") or {}).get("factors") or {}).get(_axis, {}).get("available") and not (((_row.get("current") or {}).get("factors") or {}).get(_axis, {}).get("proxy")))
            for _axis in ("earnings_momentum", "demand_cycle", "pricing_margin", "financial_conditions", "market_internals", "valuation")
        },
        "core_current_direct_industry_count": _core_direct_industries,
        "core_current_direct_industry_pct": round((_core_direct_industries / len(results) * 100.0), 1) if results else 0.0,
        "core_current_gap_industry_count": len(_core_gap_industry_keys),
        "core_current_gap_industry_keys": _core_gap_industry_keys,
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
        "api_health_path": "output/api_health.json",
        "prospective_validation": prospective_after,
        "oos_bridge_status": oos_status,
        "oos_bridge_evaluated_cases": oos_cases,
        "oos_bridge_max_adjustment_points": oos_limits["max_points"],
        "oos_bridge_note": oos_health_note,
    })
    _append_history(root, as_of, results)
    api_health = flush_api_health(root)
    overall["api_health"] = {
        "path": "output/api_health.json",
        "totals": api_health.get("totals") or {},
        "states": ["LIVE", "CACHE", "LKG", "FALLBACK", "UNAVAILABLE"],
    }
    # Keep the canonical JSON keys/paths intact; this is additive metadata only.
    write_json(output_dir / "industry_environment_latest.json", overall)
    return overall
