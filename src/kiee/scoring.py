from __future__ import annotations

import math
from statistics import mean
from typing import Any

from .util import clamp, finite, find_month_row, nested, roundn, weighted_average
from .regime_detector import detect_regime, apply_regime_weights

FACTOR_ORDER = [
    "earnings_momentum",
    "demand_cycle",
    "pricing_margin",
    "financial_conditions",
    "market_internals",
    "valuation",
]

FACTOR_LABELS = {
    "earnings_momentum": "산업 실적 모멘텀",
    "demand_cycle": "수요·경기",
    "pricing_margin": "가격·마진",
    "financial_conditions": "금융환경",
    "market_internals": "증시 내부환경",
    "valuation": "밸류에이션",
}

# ── OOS 상태에 따른 bridge 허용 한도 ───────────────────────────────────────
_OOS_BRIDGE_LIMITS = {
    "PENDING":  {"max_points": 2.0,  "allowed_auxiliary": False, "allowed_primary": False},
    "TESTING":  {"max_points": 2.0,  "allowed_auxiliary": True,  "allowed_primary": False},
    "PASSED":   {"max_points": 10.0, "allowed_auxiliary": True,  "allowed_primary": True},
}

# coverage 상한에 따른 quality 캡
def _coverage_quality_cap(coverage_pct: float) -> float:
    """커버리지가 낮을수록 quality 상한을 강하게 제한"""
    if coverage_pct <= 0:
        return 0.0
    if coverage_pct <= 10:
        return 35.0
    if coverage_pct <= 20:
        return 48.0
    if coverage_pct <= 35:
        return 60.0
    if coverage_pct <= 50:
        return 72.0
    if coverage_pct <= 70:
        return 85.0
    return 100.0


def _score_band(score: float, policy: dict[str, Any]) -> str:
    rounded = int(round(clamp(score, 0, 100)))
    for row in policy.get("score_bands") or []:
        if int(row.get("min", 0)) <= rounded <= int(row.get("max", 100)):
            return str(row.get("label") or "중립")
    return "중립"


def _delta_strength(delta: float, policy: dict[str, Any]) -> str:
    value = abs(delta)
    for row in policy.get("delta_bands") or []:
        if value <= float(row.get("abs_max", 999)):
            return str(row.get("label") or "")
    return ""


def _q_from_gate(gate: Any, passed_q: float = 90.0, failed_q: float = 55.0) -> float:
    if not isinstance(gate, dict):
        return failed_q
    return passed_q if gate.get("passed") is True else failed_q


def _normalize_macro_score(value: Any, span: float = 25.0) -> float | None:
    number = finite(value)
    if number is None:
        return None
    return clamp(50.0 + (number - 50.0) * (25.0 / max(span, 1e-6)), 0.0, 100.0) if span != 25 else clamp(number, 0.0, 100.0)


def _broad_normalized_component(korea_equity: dict[str, Any], key: str, shrink: float) -> tuple[float | None, float]:
    value = finite(nested(korea_equity, "components", key, "score_normalized"))
    available = nested(korea_equity, "components", key, "available") is True
    if value is None or not available:
        return None, 0.0
    raw = clamp(50.0 + 50.0 * value, 0.0, 100.0)
    return 50.0 + (raw - 50.0) * shrink, 45.0


def _theme_signal(industry: dict[str, Any], boom: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    relevance = clamp(finite(industry.get("theme_relevance"), 1.0) or 0.0, 0.0, 1.0)
    wanted = set(str(x) for x in (industry.get("theme_ids") or []))
    rows = [row for row in (boom.get("decisions") or []) if isinstance(row, dict) and str(row.get("theme_id")) in wanted]
    if not rows or relevance <= 0:
        return {"available": False, "current": None, "leading_3m": None, "quality": 0.0, "themes": [], "relevance_weight": relevance}
    shrink = 1.0 if boom.get("investment_use_allowed") is True else float(policy.get("theme_prevalidation_shrinkage", 0.55))
    cap = 100.0 if boom.get("investment_use_allowed") is True else float(policy.get("theme_prevalidation_quality_cap", 65))
    currents: list[float] = []
    leads: list[float] = []
    qualities: list[float] = []
    current_components: dict[str, list[float]] = {key: [] for key in ("industrial", "commercialization", "investment", "diffusion", "boom")}
    leading_components: dict[str, list[float]] = {key: [] for key in ("industrial", "commercialization", "investment", "diffusion", "boom")}
    details: list[dict[str, Any]] = []
    for row in rows:
        industrial = finite(row.get("industrial_signal_score"), 50.0) or 50.0
        commercialization = finite(row.get("direct_commercialization_score"), 50.0) or 50.0
        investment = finite(row.get("phase3_investment_score"), 50.0) or 50.0
        diffusion = finite(row.get("source_diffusion_percent"), 50.0) or 50.0
        boom_score = finite(row.get("boom_score"), 50.0) or 50.0
        predicted = finite(row.get("predicted_score"), industrial) or industrial
        momentum3 = clamp(finite(row.get("score_change_3m"), 0.0) or 0.0, -12.0, 12.0)
        current_raw = 0.35 * industrial + 0.25 * commercialization + 0.20 * investment + 0.10 * diffusion + 0.10 * boom_score
        lead_raw = 0.45 * predicted + 0.20 * commercialization + 0.20 * investment + 0.10 * diffusion + 0.05 * boom_score + momentum3 * 0.25
        current = clamp(50.0 + (current_raw - 50.0) * shrink * relevance, 0.0, 100.0)
        leading = clamp(50.0 + (lead_raw - 50.0) * shrink * relevance, 0.0, 100.0)
        component_values = {"industrial": industrial, "commercialization": commercialization, "investment": investment, "diffusion": diffusion, "boom": boom_score}
        for component_key, component_value in component_values.items():
            adjusted = clamp(50.0 + (component_value - 50.0) * shrink * relevance, 0.0, 100.0)
            current_components[component_key].append(adjusted)
        leading_values = {"industrial": predicted, "commercialization": commercialization, "investment": investment, "diffusion": diffusion, "boom": boom_score}
        for component_key, component_value in leading_values.items():
            adjusted = clamp(50.0 + (component_value - 50.0) * shrink * relevance, 0.0, 100.0)
            leading_components[component_key].append(adjusted)
        base_quality = min(cap, 45.0 + 0.45 * diffusion)
        quality = min(cap, base_quality * (0.45 + 0.55 * relevance))
        currents.append(current)
        leads.append(leading)
        qualities.append(quality)
        details.append({
            "theme_id": row.get("theme_id"), "theme_name": row.get("theme_name"),
            "current_score": roundn(current, 2), "leading_score_3m": roundn(leading, 2),
            "source_diffusion_percent": roundn(diffusion, 1), "prevalidation": boom.get("investment_use_allowed") is not True,
            "industry_theme_relevance": roundn(relevance, 2),
        })
    return {
        "available": True,
        "current": mean(currents),
        "leading_3m": mean(leads),
        "quality": min(cap, mean(qualities)),
        "themes": details,
        "current_components": {key: roundn(mean(values), 2) for key, values in current_components.items() if values},
        "leading_components": {key: roundn(mean(values), 2) for key, values in leading_components.items() if values},
        "prevalidation": boom.get("investment_use_allowed") is not True,
        "relevance_weight": roundn(relevance, 2),
    }


def _extract_global(global_bundle: dict[str, Any]) -> dict[str, Any]:
    cards = global_bundle.get("cards") or {}
    c8 = cards.get("8") or cards.get(8) or {}
    c9 = cards.get("9") or cards.get(9) or {}
    c10 = cards.get("10") or cards.get(10) or {}
    c11 = cards.get("11") or cards.get(11) or {}
    c12 = cards.get("12") or cards.get(12) or {}
    eq_current = 50.0 + 10.0 * (finite(nested(c12, "group_scores", "equity"), 0.0) or 0.0)
    eq_3m_row = nested(c12, "predictive_validation", "groups", "equity", "forecasts", "3m", default={}) or {}
    eq_3m = finite(eq_3m_row.get("forecast"), eq_current)
    card11_raw = finite(c11.get("score"), 0.0) or 0.0
    macro_current = clamp(50.0 + card11_raw * 1.5, 0.0, 100.0)
    return {
        "consumer_current": clamp(finite(c9.get("current"), 50.0) or 50.0, 0, 100),
        "consumer_3m": clamp(finite(c9.get("forecast_3m"), c9.get("current")) or 50.0, 0, 100),
        "consumer_3m_quality": _q_from_gate(nested(c9, "forecasts", "3m", "quality_gate", default={})),
        "cost_pressure_current": clamp(finite(c10.get("current"), 50.0) or 50.0, 0, 100),
        "cost_pressure_3m": clamp(finite(c10.get("forecast_3m"), c10.get("current")) or 50.0, 0, 100),
        "cost_3m_quality": _q_from_gate(nested(c10, "forecasts", "3m", "quality_gate", default={})),
        "equity_current": clamp(eq_current, 0, 100),
        "equity_3m": clamp(eq_3m if eq_3m is not None else eq_current, 0, 100),
        "equity_3m_quality": _q_from_gate(eq_3m_row.get("quality_gate") or {}),
        "macro_current": macro_current,
        "macro_quality": 80.0 if nested(c11, "quality_gate", "passed") is True else 55.0,
        "us10y": finite(nested(c8, "current", "DGS10", "value")),
        "us_real10y": finite(nested(c8, "current", "DFII10", "value")),
    }


def _extract_korea(korea_rate: dict[str, Any], korea_equity: dict[str, Any]) -> dict[str, Any]:
    rate_current = finite(nested(korea_rate, "rate", "current_rate_pct"))
    rate_3m = finite(nested(korea_rate, "rate", "calendar_horizon_estimates", "3m"), rate_current)
    rate_quality = finite(nested(korea_rate, "rate", "quality_gate", "forecast_quality_score"), 70.0) or 70.0
    fx_current = finite(nested(korea_rate, "fx", "current_usdkrw"))
    fx3 = find_month_row(nested(korea_rate, "fx", "forecast_path", default=[]), 3)
    fx_3m = finite(fx3.get("point_forecast"), fx_current)
    fx_quality = finite(fx3.get("model_quality_score"), 60.0) or 60.0
    liq_current = finite(nested(korea_rate, "krw_liquidity", "current", "liquidity_score"), 0.0) or 0.0
    liq3 = find_month_row(nested(korea_rate, "krw_liquidity", "forecast_path", default=[]), 3)
    liq_3m = finite(liq3.get("liquidity_score"), liq_current) or liq_current
    liq_quality = finite(liq3.get("forecast_quality_score"), 75.0) or 75.0
    strength_current = finite(nested(korea_rate, "krw_strength", "current", "strength_score"), 50.0) or 50.0
    strength3 = find_month_row(nested(korea_rate, "krw_strength", "forecast_path", default=[]), 3)
    strength_3m = finite(strength3.get("strength_score"), strength_current) or strength_current
    strength_quality = finite(strength3.get("independent_oos_quality_score"), strength3.get("model_quality_score")) or 60.0
    gov3y = finite(nested(korea_equity, "current_inputs", "credit", "gov_3y_pct"))
    credit = finite(nested(korea_equity, "components", "credit_spread", "score_normalized"), 0.0) or 0.0
    return {
        "rate_current": rate_current, "rate_3m": rate_3m, "rate_quality_3m": clamp(rate_quality, 0, 100),
        "fx_current": fx_current, "fx_3m": fx_3m, "fx_quality_3m": clamp(fx_quality, 0, 100),
        "liquidity_current": clamp(liq_current, -1, 1), "liquidity_3m": clamp(liq_3m, -1, 1), "liquidity_quality_3m": clamp(liq_quality, 0, 100),
        "strength_current": clamp(strength_current, 0, 100), "strength_3m": clamp(strength_3m, 0, 100), "strength_quality_3m": clamp(strength_quality, 0, 100),
        "gov3y": gov3y, "credit_health": clamp(credit, -1, 1),
        "equity_score": clamp(finite(korea_equity.get("score"), 50.0) or 50.0, 0, 100),
    }


def _financial_score(industry: dict[str, Any], kr: dict[str, Any], future: bool) -> tuple[float, float, dict[str, Any]]:
    sens = industry.get("sensitivities") or {}
    rate_level_parts = []
    if kr.get("rate_current") is not None:
        rate_level_parts.append(clamp((3.25 - float(kr["rate_current"])) / 1.5, -1, 1))
    if kr.get("gov3y") is not None:
        rate_level_parts.append(clamp((3.75 - float(kr["gov3y"])) / 1.5, -1, 1))
    rate_level = mean(rate_level_parts) if rate_level_parts else 0.0
    if future and kr.get("rate_current") is not None and kr.get("rate_3m") is not None:
        rate_change = clamp((float(kr["rate_current"]) - float(kr["rate_3m"])) / 0.50, -1, 1)
        rate_impulse = 0.45 * rate_level + 0.55 * rate_change
        rate_quality = kr.get("rate_quality_3m", 60.0)
    else:
        rate_impulse = rate_level
        rate_quality = 85.0
    strength = float(kr.get("strength_3m") if future else kr.get("strength_current") or 50.0)
    krw_weakness = clamp((50.0 - strength) / 50.0, -1, 1)
    liquidity = float(kr.get("liquidity_3m") if future else kr.get("liquidity_current") or 0.0)
    credit = float(kr.get("credit_health") or 0.0)
    pieces = [
        (rate_impulse, abs(float(sens.get("rate_relief", 0.0))), float(sens.get("rate_relief", 0.0)), rate_quality),
        (krw_weakness, abs(float(sens.get("krw_weakness", 0.0))), float(sens.get("krw_weakness", 0.0)), kr.get("strength_quality_3m", 80.0) if future else 85.0),
        (liquidity, abs(float(sens.get("liquidity", 0.0))), float(sens.get("liquidity", 0.0)), kr.get("liquidity_quality_3m", 80.0) if future else 90.0),
        (credit, abs(float(sens.get("credit_health", 0.0))), float(sens.get("credit_health", 0.0)), 85.0 if not future else 70.0),
    ]
    num = den = qnum = 0.0
    for impulse, magnitude, signed_sensitivity, quality in pieces:
        if magnitude <= 0:
            continue
        num += impulse * signed_sensitivity
        den += magnitude
        qnum += float(quality) * magnitude
    if den <= 0:
        return 50.0, 40.0, {"rate_impulse": rate_impulse, "krw_weakness": krw_weakness, "liquidity": liquidity, "credit": credit}
    normalized = clamp(num / den, -1, 1)
    return 50.0 + 50.0 * normalized, clamp(qnum / den, 0, 100), {
        "rate_impulse": roundn(rate_impulse, 4), "krw_weakness": roundn(krw_weakness, 4),
        "liquidity": roundn(liquidity, 4), "credit_health": roundn(credit, 4),
    }


def _factor(value: float | None, quality: float, source: str, detail: str = "", proxy: bool = False) -> dict[str, Any]:
    return {
        "score": roundn(clamp(value, 0, 100), 2) if value is not None else None,
        "quality": roundn(clamp(quality, 0, 100), 1),
        "available": value is not None and quality > 0,
        "source": source,
        "detail": detail,
        "proxy": proxy,
    }


def _combine_factor(pieces: list[tuple[float | None, float, float]], source: str, detail: str, proxy: bool = False) -> dict[str, Any]:
    valid = [(s, w, q) for s, w, q in pieces if s is not None and w > 0 and q > 0]
    if not valid:
        return _factor(None, 0.0, source, detail, proxy)
    effective = [(float(s), float(w) * float(q) / 100.0) for s, w, q in valid]
    score, den = weighted_average(effective, 50.0)
    q = sum(float(q) * float(w) for _, w, q in valid) / sum(float(w) for _, w, _ in valid)
    return _factor(score, q, source, detail, proxy)


def _build_factors(industry: dict[str, Any], policy: dict[str, Any], kr: dict[str, Any], gl: dict[str, Any], korea_equity: dict[str, Any], boom: dict[str, Any], direct: dict[str, Any], future: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    shrink = float(policy.get("broad_proxy_deviation_shrinkage", 0.35))
    theme = _theme_signal(industry, boom, policy)
    theme_score = theme.get("leading_3m") if future else theme.get("current")
    theme_q = float(theme.get("quality") or 0.0)

    if not future:
        current_components = theme.get("current_components") or {}
        earnings = _combine_factor(
            [(finite(current_components.get("industrial")), 0.60, theme_q), (finite(current_components.get("commercialization")), 0.40, theme_q)],
            "산업 고유 실적·상업화 신호",
            "현재 산업점수에는 해당 산업의 실물·상업화 신호만 사용합니다.",
            proxy=not theme.get("available"),
        )
        demand = _combine_factor(
            [(finite(current_components.get("industrial")), 0.60, theme_q), (finite(current_components.get("diffusion")), 0.40, theme_q)],
            "산업 고유 실물·확산 신호",
            "현재 수요·경기 축은 해당 산업 실물신호와 산업 확산도만 반영합니다.",
            proxy=not theme.get("available"),
        )
        pricing = _combine_factor(
            [(finite(current_components.get("commercialization")), 0.55, theme_q), (finite(current_components.get("investment")), 0.45, theme_q)],
            "산업 고유 상업화·투자 신호",
            "현재 가격·마진 축은 해당 산업의 상업화와 투자 신호만 반영합니다.",
            proxy=not theme.get("available"),
        )
        # 금융환경: 현재 점수에서도 민감도가 있으면 kr 데이터로 산출 (estimated 레이어)
        fin_score, fin_q, fin_detail = _financial_score(industry, kr, False)
        sens = industry.get("sensitivities") or {}
        has_fin_sens = any(abs(float(sens.get(k, 0))) > 0.05 for k in ("rate_relief", "krw_weakness", "liquidity", "credit_health"))
        if has_fin_sens:
            financial = _factor(fin_score, fin_q * 0.75, "한국 금리·원화·유동성·신용 (현재 민감도 추정)", str(fin_detail), proxy=True)
        else:
            financial = _factor(None, 0.0, "산업 고유 금융환경 지표 연결 대기", "금리·환율 민감도가 낮아 제외")

        direct_score = finite(direct.get("market_internal_score"))
        direct_q = finite(direct.get("market_internal_quality"), 0.0) or 0.0
        market_internal = _combine_factor(
            [(direct_score, 1.0, direct_q)],
            "해당 산업 KRX 대표바스켓 내부환경",
            "현재 산업점수에는 해당 산업 대표바스켓의 수익률·상승 breadth·수급만 반영합니다.",
            proxy=direct_score is None,
        )
        direct_val = finite(direct.get("valuation_score"))
        direct_val_q = finite(direct.get("valuation_quality"), 0.0) or 0.0
        valuation = _combine_factor(
            [(direct_val, 1.0, direct_val_q)],
            "해당 산업 대표바스켓 PER·PBR",
            "현재 산업점수에는 해당 산업 대표바스켓의 상대 밸류에이션만 사용합니다.",
            proxy=direct_val is None,
        )
        factors = {
            "earnings_momentum": earnings,
            "demand_cycle": demand,
            "pricing_margin": pricing,
            "financial_conditions": financial,
            "market_internals": market_internal,
            "valuation": valuation,
        }
        return factors, {
            "theme": theme,
            "financial_detail": fin_detail if has_fin_sens else {},
            "current_definition": "industry_only",
            "forecast_definition": "industry_plus_sensitive_macro",
        }

    broad_earn, broad_earn_q = _broad_normalized_component(korea_equity, "earnings_revision", shrink)
    earnings = _combine_factor(
        [(theme_score, 0.80 if theme.get("available") else 0.0, theme_q), (broad_earn, 0.20 if theme.get("available") else 1.0, broad_earn_q * (0.75 if future else 1.0))],
        "산업붐 실물·상업화 + 한국시장 이익환경 대용치",
        "테마 연결 산업은 실물·상업화 신호를 우선하고, 미연결 산업은 한국시장 후행 EPS 대용치를 축소 사용합니다.",
        proxy=not theme.get("available"),
    )

    consumer = gl["consumer_3m"] if future else gl["consumer_current"]
    consumer_q = gl["consumer_3m_quality"] if future else 95.0
    macro = gl["macro_current"]
    macro_q = gl["macro_quality"] * (0.85 if future else 1.0)
    consumer_sens = abs(float((industry.get("sensitivities") or {}).get("consumer_cycle", 0.0)))
    global_demand = clamp(50.0 + (consumer - 50.0) * min(1.0, consumer_sens) + (macro - 50.0) * 0.25, 0, 100)
    demand = _combine_factor(
        [(theme_score, 0.65 if theme.get("available") else 0.0, theme_q), (global_demand, 0.35 if theme.get("available") else 1.0, min(consumer_q, macro_q))],
        "산업 선행 실물신호 + 글로벌 고용·소비·경기",
        "산업 테마의 실물·사업화 흐름과 글로벌 수요 환경을 산업별 소비민감도로 결합합니다.",
        proxy=not theme.get("available"),
    )

    cost_pressure = gl["cost_pressure_3m"] if future else gl["cost_pressure_current"]
    cost_q = gl["cost_3m_quality"] if future else 95.0
    cost_sens = float((industry.get("sensitivities") or {}).get("cost_relief", 0.0))
    cost_score = clamp(50.0 - (cost_pressure - 50.0) * cost_sens, 0, 100)
    pricing = _combine_factor(
        [(theme_score, 0.45 if theme.get("available") else 0.0, theme_q), (cost_score, 0.55 if theme.get("available") else 1.0, cost_q)],
        "산업 마진·상업화 + 글로벌 원가압력",
        "원가 소비 산업과 원자재 생산 산업의 방향을 반대로 적용합니다.",
        proxy=not theme.get("available"),
    )

    fin_score, fin_q, fin_detail = _financial_score(industry, kr, future)
    financial = _factor(fin_score, fin_q, "한국 금리·원화·유동성·신용", str(fin_detail), False)

    direct_score = finite(direct.get("market_internal_score"))
    direct_q = finite(direct.get("market_internal_quality"), 0.0) or 0.0
    broad_market = kr["equity_score"]
    global_equity = gl["equity_3m"] if future else gl["equity_current"]
    global_eq_q = gl["equity_3m_quality"] if future else 90.0
    if future:
        global_equity_sens = clamp(abs(float((industry.get("sensitivities") or {}).get("global_equity", 0.0))), 0.0, 1.0)
        global_equity_weight = 0.10 + 0.35 * global_equity_sens
        direct_weight = 0.60 - 0.25 * global_equity_sens
        broad_weight = 0.85 - global_equity_weight - direct_weight
        market_pieces = [(direct_score, direct_weight, direct_q * 0.70), (broad_market, broad_weight, 80.0), (global_equity, global_equity_weight, global_eq_q), (theme_score, 0.15 if theme.get("available") else 0.0, theme_q)]
    else:
        market_pieces = [(direct_score, 0.65, direct_q), (broad_market, 0.25, 90.0), (global_equity, 0.10, 90.0)]
    market_internal = _combine_factor(
        market_pieces,
        "KRX 대표바스켓 + 한국증시 직접환경 + 글로벌 주식환경",
        "KRX는 시장 전체 표를 소수 회 호출한 뒤 업종 대표바스켓만 로컬 필터링합니다.",
        proxy=direct_score is None,
    )

    direct_val = finite(direct.get("valuation_score"))
    direct_val_q = finite(direct.get("valuation_quality"), 0.0) or 0.0
    history_ready = direct.get("valuation_history_ready") is True
    history_samples = int(direct.get("valuation_history_samples") or 0)
    broad_val, broad_val_q = _broad_normalized_component(korea_equity, "valuation", shrink)
    direct_weight = 0.90 if history_ready else 0.65
    broad_weight = 0.10 if history_ready else 0.35
    valuation = _combine_factor(
        [(direct_val, direct_weight if direct_val is not None else 0.0, direct_val_q * (0.75 if future else 1.0)), (broad_val, broad_weight if direct_val is not None else 1.0, broad_val_q * (0.65 if future else 1.0))],
        "산업 자체 PER·PBR 역사 + KRX 대표바스켓 + 한국시장 가치환경",
        (
            f"동일 산업 주간 PER/PBR 역사표본 {history_samples}개를 우선 사용합니다."
            if history_ready else
            f"산업 자체 역사표본이 아직 {history_samples}개라 초기구간입니다."
        ),
        proxy=not history_ready,
    )

    factors = {
        "earnings_momentum": earnings,
        "demand_cycle": demand,
        "pricing_margin": pricing,
        "financial_conditions": financial,
        "market_internals": market_internal,
        "valuation": valuation,
    }
    return factors, {
        "theme": theme,
        "financial_detail": fin_detail,
        "current_definition": "industry_only",
        "forecast_definition": "industry_plus_sensitive_macro",
    }


def _aggregate_score(factors: dict[str, Any], weights: dict[str, Any]) -> dict[str, Any]:
    base_available = 0.0
    quality_weighted = 0.0
    num = 0.0
    den = 0.0
    contributions: list[dict[str, Any]] = []
    for key in FACTOR_ORDER:
        base = float(weights.get(key, 0.0))
        item = factors.get(key) or {}
        score = finite(item.get("score"))
        quality = clamp(finite(item.get("quality"), 0.0) or 0.0, 0, 100)
        if score is None or quality <= 0 or base <= 0:
            continue
        base_available += base
        effective = base * quality / 100.0
        quality_weighted += effective
        num += score * effective
        den += effective
    raw = num / den if den else 50.0
    shrink = clamp(quality_weighted / 0.65, 0.0, 1.0)
    final = 50.0 + (raw - 50.0) * shrink
    for key in FACTOR_ORDER:
        base = float(weights.get(key, 0.0))
        item = factors.get(key) or {}
        score = finite(item.get("score"))
        quality = clamp(finite(item.get("quality"), 0.0) or 0.0, 0, 100)
        effective = base * quality / 100.0 if score is not None else 0.0
        normalized_weight = effective / den if den else 0.0
        contribution = (float(score) - 50.0) * normalized_weight if score is not None else 0.0
        contributions.append({
            "factor": key, "label": FACTOR_LABELS[key], "score": roundn(score, 2), "quality": roundn(quality, 1),
            "base_weight": round(base, 4), "effective_weight": round(effective, 4), "normalized_weight": round(normalized_weight, 4),
            "contribution_points": round(contribution, 2), "available": score is not None,
        })
    return {
        "score": clamp(final, 0, 100), "raw_score": clamp(raw, 0, 100),
        "base_data_coverage_pct": clamp(base_available * 100.0, 0, 100),
        "quality_weighted_coverage_pct": clamp(quality_weighted * 100.0, 0, 100),
        "neutral_shrinkage": round(shrink, 4), "contributions": contributions,
    }


def _sector_specificity(theme: dict[str, Any], direct: dict[str, Any]) -> float:
    has_theme = theme.get("available") is True
    has_direct = finite(direct.get("market_internal_score")) is not None
    has_val = finite(direct.get("valuation_score")) is not None
    if has_theme and has_direct and has_val:
        return 95.0
    if has_theme and has_direct:
        return 88.0
    if has_direct:
        return 80.0
    if has_theme:
        return 70.0
    return 45.0


def _quality_score(aggregate: dict[str, Any], specificity: float, freshness: float, future: bool, forecast_upstream_quality: float, prospective_quality: float, coverage_pct: float = 100.0) -> float:
    qcov = float(aggregate.get("quality_weighted_coverage_pct") or 0.0)
    cap = _coverage_quality_cap(coverage_pct)
    if future:
        raw = clamp(0.40 * qcov + 0.22 * specificity + 0.20 * forecast_upstream_quality + 0.10 * freshness + 0.08 * prospective_quality, 0, 100)
    else:
        raw = clamp(0.58 * qcov + 0.27 * specificity + 0.15 * freshness, 0, 100)
    return clamp(raw, 0, cap)


def _top_reasons(contributions: list[dict[str, Any]], positive: bool) -> list[dict[str, Any]]:
    rows = [r for r in contributions if (float(r.get("contribution_points") or 0) > 0 if positive else float(r.get("contribution_points") or 0) < 0)]
    rows.sort(key=lambda r: abs(float(r.get("contribution_points") or 0)), reverse=True)
    return [{"factor": r["factor"], "label": r["label"], "points": r["contribution_points"], "score": r["score"]} for r in rows[:4]]


def _industry_cycle_row(industry_cycle: dict[str, Any], industry_key: str) -> dict[str, Any] | None:
    if not isinstance(industry_cycle, dict):
        return None
    rows = industry_cycle.get("industries") or []
    if isinstance(rows, dict):
        candidate = rows.get(industry_key)
        return candidate if isinstance(candidate, dict) else None
    for row in rows:
        if isinstance(row, dict) and str(row.get("industry_key") or row.get("key") or "") == industry_key:
            return row
    return None


def _pending_stage(reason: str) -> dict[str, Any]:
    factors = {key: _factor(None, 0.0, "산업실물지표 연결 대기", reason) for key in FACTOR_ORDER}
    return {
        "score": None,
        "band": "데이터 부족",
        "quality_score": 0.0,
        "data_coverage_pct": 0.0,
        "quality_weighted_coverage_pct": 0.0,
        "factors": factors,
        "contributions": [],
        "status": "insufficient_data",
        "reason": reason,
    }


def _apply_dart_earnings_to_factors(
    factors: dict[str, Any],
    dart_metric: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    DART 분기 영업이익 YoY를 earnings_momentum 팩터에 반영.
    기존 팩터가 있으면 blending(DART 35%), 없으면 단독 사용.
    """
    if not dart_metric or not isinstance(dart_metric, dict):
        return factors
    d_score = finite(dart_metric.get("score"))
    d_quality = finite(dart_metric.get("quality"), 0.0) or 0.0
    if d_score is None or d_quality <= 0:
        return factors

    factors = dict(factors)
    em = factors.get("earnings_momentum") or {}
    surprise = dart_metric.get("surprise", False)

    if em.get("available") and em.get("score") is not None:
        old_score = float(em["score"])
        # DART를 35% 가중 blending, 서프라이즈이면 45%
        dart_weight = 0.45 if surprise else 0.35
        blended = old_score * (1 - dart_weight) + d_score * dart_weight
        factors["earnings_momentum"] = dict(em)
        factors["earnings_momentum"]["score"] = roundn(blended, 2)
        factors["earnings_momentum"]["quality"] = clamp(
            em.get("quality", 0) * 0.6 + d_quality * 0.4, 0, 100
        )
        factors["earnings_momentum"]["source"] = em.get("source", "") + " + DART 분기 OP YoY"
        factors["earnings_momentum"]["dart_earnings_applied"] = True
        if surprise:
            factors["earnings_momentum"]["surprise"] = True
    else:
        factors["earnings_momentum"] = _factor(
            d_score, d_quality * 0.85,
            "DART 공시 분기 영업이익 YoY",
            f"분기 YoY {dart_metric.get('median_yoy_pct',0):.1f}% ({dart_metric.get('n_firms',0)}개사)",
            proxy=False,
        )
        if surprise:
            factors["earnings_momentum"]["surprise"] = True
    return factors


def _apply_nowcast_to_factors(
    factors: dict[str, Any],
    nowcast_metric: dict[str, Any] | None,
    industry: dict[str, Any],
) -> dict[str, Any]:
    """
    관세청 nowcast 지표를 estimated 팩터에 반영.
    - production_shipments → earnings_momentum 보강
    - 수출 비중이 높은 산업은 demand_cycle에도 부분 반영
    품질은 QUALITY_CAP(65) 상한, shrinkage 적용된 점수 사용.
    """
    if not nowcast_metric or not isinstance(nowcast_metric, dict):
        return factors
    nc_score = finite(nowcast_metric.get("score"))
    nc_quality = finite(nowcast_metric.get("quality"), 0.0) or 0.0
    if nc_score is None or nc_quality <= 0:
        return factors

    factors = dict(factors)
    sens = industry.get("sensitivities") or {}
    export_sens = clamp(abs(float(sens.get("krw_weakness", 0.0))), 0.0, 1.0)  # 원화약세 민감도 ≈ 수출 의존도 proxy

    # earnings_momentum 보강 (수출이 실적 선행지표)
    em = factors.get("earnings_momentum") or {}
    if em.get("available") and em.get("score") is not None:
        # 기존 팩터와 blending — nowcast는 보조(25% 비중)
        old_score = float(em["score"])
        blended = old_score * 0.75 + nc_score * 0.25
        factors["earnings_momentum"] = dict(em)
        factors["earnings_momentum"]["score"] = roundn(blended, 2)
        factors["earnings_momentum"]["source"] = em.get("source", "") + " + 관세청 수출 nowcast"
        factors["earnings_momentum"]["nowcast_applied"] = True
    elif not em.get("available"):
        # 팩터 자체가 없었으면 nowcast 단독 사용 (낮은 quality)
        factors["earnings_momentum"] = _factor(nc_score, min(nc_quality * 0.6, 35.0),
            "관세청 수출입 속보 nowcast (단독)", "실물 팩터 없음 — nowcast 보조치만 사용", proxy=True)

    # demand_cycle: 수출 의존도 높은 산업(export_sens > 0.5)에만 부분 반영
    if export_sens >= 0.5:
        dc = factors.get("demand_cycle") or {}
        if dc.get("available") and dc.get("score") is not None:
            old_dc = float(dc["score"])
            blended_dc = old_dc * 0.8 + nc_score * 0.2
            factors["demand_cycle"] = dict(dc)
            factors["demand_cycle"]["score"] = roundn(blended_dc, 2)
            factors["demand_cycle"]["nowcast_applied"] = True
        elif not dc.get("available"):
            factors["demand_cycle"] = _factor(nc_score, min(nc_quality * 0.5, 30.0),
                "관세청 수출 nowcast → 수요 대용치", "수출 민감도 높은 산업 한정", proxy=True)

    return factors


def _build_estimated_current(
    industry: dict[str, Any],
    policy: dict[str, Any],
    kr: dict[str, Any],
    gl: dict[str, Any],
    korea_equity: dict[str, Any],
    boom: dict[str, Any],
    direct: dict[str, Any],
    nowcast_metric: dict[str, Any] | None = None,
    regime: dict[str, Any] | None = None,
    dart_metric: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    observed 데이터가 없을 때 매크로+테마+KRX로 추정 현재 점수를 산출.
    nowcast_metric: 관세청 수출입 속보 지표 (있으면 팩터 보강).
    단일 지표만 있으면 중립 방향으로 수축. 완전 데이터 없으면 None 반환.
    """
    factors, meta = _build_factors(industry, policy, kr, gl, korea_equity, boom, direct, False)
    # nowcasting 반영
    if nowcast_metric:
        factors = _apply_nowcast_to_factors(factors, nowcast_metric, industry)
    # DART 분기 영업이익 반영
    if dart_metric:
        factors = _apply_dart_earnings_to_factors(factors, dart_metric)
    # 레짐 가중치 적용 (factors 산출 후 aggregate 시 사용)
    available_count = sum(1 for f in factors.values() if f.get("available"))
    if available_count == 0:
        return None

    base_weights = industry.get("weights_current") or {}
    weights = apply_regime_weights(base_weights, regime) if regime else base_weights
    agg = _aggregate_score(factors, weights)

    # 단일 지표 과의존 방지: 지표 1~2개면 50 방향 강하게 수축
    if available_count <= 1:
        shrink_extra = 0.3
    elif available_count == 2:
        shrink_extra = 0.6
    else:
        shrink_extra = 1.0

    raw_score = float(agg["score"])
    score = 50.0 + (raw_score - 50.0) * shrink_extra

    # 추정 품질: 커버리지 + 지표수 + proxy 여부 반영
    proxy_count = sum(1 for f in factors.values() if f.get("proxy") and f.get("available"))
    proxy_penalty = proxy_count * 8.0
    estimated_q = max(0.0, min(55.0, 20.0 + available_count * 8.0 - proxy_penalty))

    return {
        "score": clamp(score, 0, 100),
        "raw_score": clamp(raw_score, 0, 100),
        "base_data_coverage_pct": agg["base_data_coverage_pct"],
        "quality_weighted_coverage_pct": agg["quality_weighted_coverage_pct"],
        "neutral_shrinkage": agg["neutral_shrinkage"],
        "available_factor_count": available_count,
        "estimated_quality": estimated_q,
        "factors": factors,
        "contributions": agg["contributions"],
        "proxy_factor_count": proxy_count,
    }


def _build_estimated_forecast(
    industry: dict[str, Any],
    policy: dict[str, Any],
    kr: dict[str, Any],
    gl: dict[str, Any],
    korea_equity: dict[str, Any],
    boom: dict[str, Any],
    direct: dict[str, Any],
    current_score: float | None,
    regime: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    관측 피드 없을 때 매크로+테마+금융환경으로 3개월 전망 추정.
    """
    factors, meta = _build_factors(industry, policy, kr, gl, korea_equity, boom, direct, True)
    available_count = sum(1 for f in factors.values() if f.get("available"))
    if available_count == 0:
        return None

    base_weights_3m = industry.get("weights_3m") or {}
    weights = apply_regime_weights(base_weights_3m, regime) if regime else base_weights_3m
    agg = _aggregate_score(factors, weights)

    if available_count <= 1:
        shrink_extra = 0.3
    elif available_count == 2:
        shrink_extra = 0.55
    else:
        shrink_extra = 1.0

    raw_score = float(agg["score"])
    score = 50.0 + (raw_score - 50.0) * shrink_extra

    # 현재 수급(market_internals)은 전망에 최대 25% 잔존
    proxy_count = sum(1 for f in factors.values() if f.get("proxy") and f.get("available"))
    proxy_penalty = proxy_count * 8.0
    # 전망 추정 quality는 현재보다 더 낮게
    estimated_q = max(0.0, min(45.0, 15.0 + available_count * 7.0 - proxy_penalty))

    delta = round(score - current_score, 1) if current_score is not None else None
    direction = "개선" if delta is not None and delta > 1 else ("악화" if delta is not None and delta < -1 else "유지") if delta is not None else "추정"

    return {
        "score": clamp(score, 0, 100),
        "raw_score": clamp(raw_score, 0, 100),
        "delta_points": delta,
        "direction": direction,
        "change_strength": _delta_strength(delta, policy) if delta is not None else "",
        "base_data_coverage_pct": agg["base_data_coverage_pct"],
        "quality_weighted_coverage_pct": agg["quality_weighted_coverage_pct"],
        "available_factor_count": available_count,
        "estimated_quality": estimated_q,
        "factors": factors,
        "contributions": agg["contributions"],
        "proxy_factor_count": proxy_count,
    }


def _pending_industry_result(
    industry: dict[str, Any],
    direct: dict[str, Any],
    reason: str,
    prospective_summary: dict[str, Any] | None = None,
    boom: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    kr: dict[str, Any] | None = None,
    gl: dict[str, Any] | None = None,
    korea_equity: dict[str, Any] | None = None,
    nowcast_metric: dict[str, Any] | None = None,
    regime: dict[str, Any] | None = None,
    dart_metric: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    observed 피드 없을 때: estimated 레이어로 fallback 시도.
    estimated도 불가하면 null 유지.
    """
    boom = boom or {}
    policy = policy or {}

    # kr=None이면 estimated 비활성 (feed_pending 상태) — or {} 변환 전에 체크
    allow_estimated = kr is not None and gl is not None and korea_equity is not None

    kr = kr or {}
    gl = gl or {}
    korea_equity = korea_equity or {}

    estimated_current = _build_estimated_current(industry, policy, kr, gl, korea_equity, boom, direct, nowcast_metric=nowcast_metric, regime=regime, dart_metric=dart_metric) if allow_estimated else None
    estimated_forecast = None
    if estimated_current is not None:
        c_score = estimated_current["score"]
        estimated_forecast = _build_estimated_forecast(industry, policy, kr, gl, korea_equity, boom, direct, c_score, regime=regime)

    theme = _theme_signal(industry, boom, policy)

    # 현재 슬롯 구성
    if estimated_current is not None:
        ec = estimated_current
        avail = ec["available_factor_count"]
        # 단일 지표 경고
        notes_warn = f"지표 {avail}개로 산출한 추정점수입니다." + (" 단일지표 의존 - 신뢰도 낮음." if avail <= 1 else "")
        current = {
            "score": round(ec["score"], 1),
            "band": _score_band(ec["score"], policy),
            "quality_score": round(ec["estimated_quality"], 1),
            "data_coverage_pct": round(ec["base_data_coverage_pct"], 1),
            "quality_weighted_coverage_pct": round(ec["quality_weighted_coverage_pct"], 1),
            "factors": ec["factors"],
            "contributions": ec["contributions"],
            "status": "estimated",
            "score_source": "estimated",
            "observed_score": None,
            "estimated_score": round(ec["score"], 1),
            "observed_coverage_pct": 0.0,
            "estimated_quality": round(ec["estimated_quality"], 1),
            "available_factor_count": avail,
            "notes": notes_warn,
        }
    else:
        current = _pending_stage(reason)
        current["score_source"] = "none"
        current["observed_score"] = None
        current["estimated_score"] = None
        current["observed_coverage_pct"] = 0.0
        current["estimated_quality"] = 0.0

    # 전망 슬롯 구성
    if estimated_forecast is not None:
        ef = estimated_forecast
        avail_f = ef["available_factor_count"]
        forecast_3m = {
            "score": round(ef["score"], 1),
            "band": _score_band(ef["score"], policy),
            "delta_points": ef["delta_points"],
            "direction": ef["direction"],
            "change_strength": ef.get("change_strength", ""),
            "quality_score": round(ef["estimated_quality"], 1),
            "data_coverage_pct": round(ef["base_data_coverage_pct"], 1),
            "quality_weighted_coverage_pct": round(ef["quality_weighted_coverage_pct"], 1),
            "factors": ef["factors"],
            "contributions": ef["contributions"],
            "top_positive_reasons": _top_reasons(ef["contributions"], True),
            "top_negative_reasons": _top_reasons(ef["contributions"], False),
            "status": "estimated",
            "score_source": "estimated",
            "estimated_score": round(ef["score"], 1),
            "estimated_quality": round(ef["estimated_quality"], 1),
            "available_factor_count": avail_f,
        }
    else:
        forecast_3m = _pending_stage(reason)
        forecast_3m["delta_points"] = None
        forecast_3m["change_strength"] = "대기"
        forecast_3m["direction"] = "대기"
        forecast_3m["top_positive_reasons"] = []
        forecast_3m["top_negative_reasons"] = []
        forecast_3m["score_source"] = "none"

    horizon_pending = {
        "score": None,
        "direction": "대기",
        "status": "insufficient_data",
        "reason": "해당 기간의 산업 선행지표가 연결되지 않아 전망을 계산하지 않았습니다.",
        "quality_score": 0.0,
        "data_coverage_pct": 0.0,
        "factors": {},
    }

    # OOS 상태 기반 bridge 한도
    oos_status = str(prospective_summary.get("status", "PENDING")) if prospective_summary else "PENDING"
    evaluated_cases = int(prospective_summary.get("evaluated_cases", 0)) if prospective_summary else 0
    bridge_limits = _oos_bridge_limits(oos_status, evaluated_cases, policy)

    model = {
        "current": "estimated_macro_theme_krx" if estimated_current else "insufficient_data",
        "future": "estimated_macro_theme_financial_conditions",
        "data_status": "estimated" if estimated_current else "insufficient_data",
        "current_inputs": industry.get("current_metric_groups") or [],
        "leading_inputs": industry.get("leading_metric_groups") or [],
        "specialized_current_metrics": industry.get("specialized_current_metrics") or [],
        "specialized_leading_metrics": industry.get("specialized_leading_metrics") or [],
    }

    return {
        "industry_key": industry["key"],
        "industry_label": industry["label"],
        "aliases": industry.get("aliases") or [],
        "current": current,
        "forecast_3m": forecast_3m,
        "forecast_3_6m": horizon_pending,
        "forecast_6_12m": horizon_pending,
        "direct_market": direct,
        "theme_bridge": theme,
        "stock_prediction_bridge": {
            "allowed_as_auxiliary": bridge_limits["allowed_auxiliary"],
            "allowed_as_primary": bridge_limits["allowed_primary"],
            "bounded_direction_adjustment_points": 0.0,
            "max_abs_adjustment_points": bridge_limits["max_points"],
            "signal_normalized": 0.0,
            "quality_score": 0.0,
            "validation_status": oos_status,
            "oos_current_limit": bridge_limits["max_points"],
            "rule": f"OOS {oos_status} (평가 {evaluated_cases}건): 허용한도 ±{bridge_limits['max_points']}pt. 산업실물피드 연결 후 본격 활성화.",
        },
        "interpretation": {
            "headline": f"추정 {current.get('band', '데이터 부족')} {current.get('score', 'N/A')}/100" if estimated_current else "산업실물지표 연결 대기",
            "beginner": reason + (" 매크로·테마·KRX 데이터로 추정 점수를 산출했습니다. 신뢰도가 낮습니다." if estimated_current else ""),
            "warning": "추정 점수(estimated)는 산업 고유 실물지표 없이 산출한 보조 참고치입니다. 과신 금지.",
        },
        "quality": {
            "sector_specificity_score": 0.0,
            "forecast_upstream_quality_score": 0.0,
            "source_freshness_score": 0.0,
            "prospective_validation": prospective_summary or {},
            "data_status": "estimated" if estimated_current else "insufficient_data",
            "current_metric_coverage": 0.0,
            "leading_metric_coverage": 0.0,
            "oos_bridge_status": oos_status,
            "oos_evaluated_cases": evaluated_cases,
            "oos_bridge_max_points": bridge_limits["max_points"],
        },
        "score_model": model,
        "notes": industry.get("notes") or "",
    }


def _oos_bridge_limits(oos_status: str, evaluated_cases: int, policy: dict[str, Any]) -> dict[str, Any]:
    """OOS 상태 및 평가 건수에 따른 bridge 허용 한도 반환"""
    min_cases = int(policy.get("prospective_min_cases", 24))
    if oos_status == "PASSED" and evaluated_cases >= min_cases:
        max_pts = float(policy.get("stock_overlay_max_points_validated", 10.0))
        return {"max_points": max_pts, "allowed_auxiliary": True, "allowed_primary": bool(policy.get("forecast_primary_use_allowed", False))}
    elif evaluated_cases >= min_cases // 2:
        # 절반 이상 검증: 제한적 허용
        return {"max_points": 2.0, "allowed_auxiliary": True, "allowed_primary": False}
    else:
        # PENDING 또는 평가 사례 부족: 최소 허용
        return {"max_points": 2.0, "allowed_auxiliary": False, "allowed_primary": False}


def _feed_stage_factors(stage: dict[str, Any], policy: dict[str, Any], kr: dict[str, Any], gl: dict[str, Any], korea_equity: dict[str, Any], boom: dict[str, Any], industry: dict[str, Any], direct: dict[str, Any], future: bool, quality: float) -> dict[str, Any]:
    """
    피드 단계의 6축 팩터 분해.
    피드에 factor_scores가 있으면 사용, 없으면 매크로·KRX로 추정 팩터 생성.
    """
    factor_scores = stage.get("factor_scores") if isinstance(stage.get("factor_scores"), dict) else {}
    has_feed_factors = bool(factor_scores)

    # factor_scores가 있어도, FACTOR_ORDER 키가 아닌 경우(예: utilization)는 직접 매핑 불가
    # FACTOR_ORDER 키가 하나라도 있으면 직접 사용, 없으면 매크로 경로로 fallback
    standard_keys = {k: finite(factor_scores.get(k)) for k in FACTOR_ORDER}
    has_standard_factors = any(v is not None for v in standard_keys.values())

    if has_feed_factors and has_standard_factors:
        factors = {}
        for key in FACTOR_ORDER:
            factor_value = standard_keys[key]
            factors[key] = _factor(
                factor_value,
                quality if factor_value is not None else 0.0,
                "산업실물지표 피드 (직접 팩터)",
                "원천 산업 피드에서 직접 제공된 팩터 점수",
                proxy=False,
            )
        return factors

    # 피드에 factor_scores 없음 → 매크로·테마·KRX로 6축 구성
    macro_factors, _ = _build_factors(industry, policy, kr, gl, korea_equity, boom, direct, future)

    # 피드 총점이 있으면, 팩터들을 피드 총점 방향으로 보정
    feed_total = finite(stage.get("score"))
    if feed_total is not None:
        # 팩터들의 가중평균을 피드 총점과 blend
        weights = industry.get("weights_3m" if future else "weights_current") or {}
        macro_agg = _aggregate_score(macro_factors, weights)
        macro_total = float(macro_agg["score"])
        blend_weight = 0.4  # 피드 총점에 40% 앵커
        if abs(macro_total - 50.0) > 0.01:
            adjustment = (feed_total - macro_total) * blend_weight
            for key, factor in macro_factors.items():
                if factor.get("available") and factor.get("score") is not None:
                    new_score = clamp(float(factor["score"]) + adjustment, 0, 100)
                    macro_factors[key] = dict(factor)
                    macro_factors[key]["score"] = roundn(new_score, 2)
                    macro_factors[key]["source"] = "산업실물피드 총점 앵커 + 매크로 팩터 추정"
                    macro_factors[key]["proxy"] = True
                    macro_factors[key]["detail"] = f"피드 총점 {feed_total:.1f}에 {blend_weight*100:.0f}% 앵커 후 매크로 팩터 추정"

    # 실물지표가 있는 축 보완: positive_indicators / negative_indicators
    pos = stage.get("positive_indicators") or []
    neg = stage.get("negative_indicators") or []
    if pos or neg:
        # 실물지표 방향 시그널로 earnings_momentum과 demand_cycle 보정
        sentiment = (len(pos) - len(neg)) / max(len(pos) + len(neg), 1)
        for key in ("earnings_momentum", "demand_cycle"):
            f = macro_factors.get(key)
            if f and f.get("available"):
                new_score = clamp(float(f["score"]) + sentiment * 5.0, 0, 100)
                macro_factors[key] = dict(f)
                macro_factors[key]["score"] = roundn(new_score, 2)

    return macro_factors


def _feed_stage(
    stage: dict[str, Any] | None,
    policy: dict[str, Any],
    horizon: str,
    current_score: float | None = None,
    industry: dict[str, Any] | None = None,
    kr: dict[str, Any] | None = None,
    gl: dict[str, Any] | None = None,
    korea_equity: dict[str, Any] | None = None,
    boom: dict[str, Any] | None = None,
    direct: dict[str, Any] | None = None,
    dart_metric: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """피드 스테이지를 렌더링. 팩터분해를 복구하고 quality를 정보량에 정직하게 연동."""
    stage = stage if isinstance(stage, dict) else {}
    score = finite(stage.get("score"))
    quality = finite(stage.get("quality_score"))
    coverage = finite(stage.get("data_coverage_pct"))
    cycle_policy = policy.get("industry_cycle_scoring") or {}
    minimum = float(cycle_policy.get("minimum_current_coverage_pct", 9.0)) if horizon == "current" else float(cycle_policy.get("minimum_leading_coverage_pct", 9.0))

    if score is None or quality is None or coverage is None or quality <= 0 or coverage < minimum:
        reason = f"{horizon} 산업실물지표 품질·커버리지 기준을 충족하지 못해 점수를 표시하지 않습니다."
        if horizon != "current" and score is None and industry and kr and gl and korea_equity is not None and boom is not None:
            # 전망 피드 없음 → estimated 전망으로 fallback
            ef = _build_estimated_forecast(industry, policy, kr, gl, korea_equity, boom, direct, current_score)
            if ef:
                avail_f = ef["available_factor_count"]
                delta = ef["delta_points"]
                return {
                    "score": round(ef["score"], 1),
                    "band": _score_band(ef["score"], policy),
                    "delta_points": delta,
                    "direction": ef["direction"],
                    "change_strength": ef.get("change_strength", ""),
                    "quality_score": round(ef["estimated_quality"], 1),
                    "data_coverage_pct": round(ef["base_data_coverage_pct"], 1),
                    "quality_weighted_coverage_pct": round(ef["quality_weighted_coverage_pct"], 1),
                    "factors": ef["factors"],
                    "contributions": ef["contributions"],
                    "metrics": stage.get("metrics") or [],
                    "positive_indicators": stage.get("positive_indicators") or [],
                    "negative_indicators": stage.get("negative_indicators") or [],
                    "status": "estimated",
                    "score_source": "estimated",
                    "estimated_score": round(ef["score"], 1),
                    "estimated_quality": round(ef["estimated_quality"], 1),
                    "available_factor_count": avail_f,
                    "top_positive_reasons": _top_reasons(ef["contributions"], True),
                    "top_negative_reasons": _top_reasons(ef["contributions"], False),
                }
        return {
            "score": None,
            "direction": "대기" if horizon != "current" else None,
            "status": "insufficient_data",
            "reason": reason,
            "quality_score": round(max(0.0, quality or 0.0), 1),
            "data_coverage_pct": round(max(0.0, coverage or 0.0), 1),
            "factors": {},
            "metrics": stage.get("metrics") or [],
        }

    score = clamp(score, 0.0, 100.0)

    # ── quality를 실제 정보량에 연동 ──────────────────────────────────────────
    # 1. coverage 상한 적용
    quality_cap = _coverage_quality_cap(coverage)
    # 2. 단일 지표 경고: metrics 수 확인
    metrics = stage.get("metrics") or []
    metric_count = len([m for m in metrics if isinstance(m, dict) and finite(m.get("value")) is not None])
    if metric_count <= 1 and metric_count > 0:
        quality = min(quality, 40.0)
        single_metric_warning = f"단일 지표({metric_count}개)로 산출 - 과의존 경고. quality 상한 40."
    else:
        single_metric_warning = None
    # 3. proxy/age penalty (피드에 age 정보 있으면 반영)
    data_age_days = finite(stage.get("data_age_days"))
    if data_age_days is not None and data_age_days > 60:
        age_penalty = min(20.0, (data_age_days - 60) * 0.3)
        quality = max(0.0, quality - age_penalty)
    # 4. 최종 quality cap 적용
    quality = min(quality, quality_cap)

    # ── 6축 팩터 분해 ─────────────────────────────────────────────────────────
    future = (horizon != "current")
    ind = industry or {}
    k = kr or {}
    g = gl or {}
    ke = korea_equity or {}
    b = boom or {}
    dm = direct or {}
    factors = _feed_stage_factors(stage, policy, k, g, ke, b, ind, dm, future, quality)
    # DART 영업이익 현재 팩터 보강 (observed 경로에서도 earnings_momentum 정밀화)
    if dart_metric and horizon == "current":
        factors = _apply_dart_earnings_to_factors(factors, dart_metric)

    # ── 총점과 팩터 기여 정보 ─────────────────────────────────────────────────
    weights = ind.get("weights_3m" if future else "weights_current") or {}
    agg = _aggregate_score(factors, weights)
    contributions = agg["contributions"]

    delta = round(score - current_score, 1) if current_score is not None and horizon != "current" else None
    direction = str(stage.get("direction") or ("개선" if delta is not None and delta > 1 else "악화" if delta is not None and delta < -1 else "유지" if delta is not None else "대기"))

    # observed 점수 - 피드에서 온 실측값
    observed_score = score
    score_source = "observed"

    result: dict[str, Any] = {
        "score": round(score, 1),
        "band": _score_band(score, policy),
        "cycle_phase": stage.get("cycle_phase"),
        "quality_score": round(clamp(quality, 0.0, 100.0), 1),
        "data_coverage_pct": round(clamp(coverage, 0.0, 100.0), 1),
        "quality_weighted_coverage_pct": round(clamp(coverage * quality / 100.0, 0.0, 100.0), 1),
        "factors": factors,
        "contributions": contributions,
        "metrics": metrics,
        "positive_indicators": stage.get("positive_indicators") or [],
        "negative_indicators": stage.get("negative_indicators") or [],
        "global_impact_score": finite(stage.get("global_impact_score")),
        "korea_impact_score": finite(stage.get("korea_impact_score")),
        "industry_leading_score": finite(stage.get("industry_leading_score")),
        "sensitivity_used": stage.get("sensitivity_used") or {},
        "direction": direction if horizon != "current" else None,
        "delta_points": delta,
        "change_strength": _delta_strength(delta, policy) if delta is not None else None,
        "status": "scored",
        "score_source": score_source,
        "observed_score": round(observed_score, 1),
        "observed_coverage_pct": round(clamp(coverage, 0.0, 100.0), 1),
        "estimated_score": None,
        "estimated_quality": None,
    }
    if single_metric_warning:
        result["notes"] = single_metric_warning
    if horizon != "current":
        result["top_positive_reasons"] = _top_reasons(contributions, True)
        result["top_negative_reasons"] = _top_reasons(contributions, False)
    return result


def _scored_industry_result_from_feed(
    industry: dict[str, Any],
    cycle_row: dict[str, Any],
    direct: dict[str, Any],
    policy: dict[str, Any],
    prospective_summary: dict[str, Any],
    boom: dict[str, Any] | None = None,
    kr: dict[str, Any] | None = None,
    gl: dict[str, Any] | None = None,
    korea_equity: dict[str, Any] | None = None,
    regime: dict[str, Any] | None = None,
    dart_metric: dict[str, Any] | None = None,
) -> dict[str, Any]:
    boom = boom or {}
    kr = kr or {}
    gl = gl or {}
    korea_equity = korea_equity or {}

    # 레짐 가중치를 industry의 weights에 반영한 임시 복사본 생성
    if regime:
        industry = dict(industry)
        industry["weights_current"] = apply_regime_weights(industry.get("weights_current") or {}, regime)
        industry["weights_3m"]      = apply_regime_weights(industry.get("weights_3m") or {}, regime)

    current = _feed_stage(
        cycle_row.get("current"), policy, "current",
        industry=industry, kr=kr, gl=gl, korea_equity=korea_equity, boom=boom, direct=direct,
        dart_metric=dart_metric if dart_metric else None,
    )
    current_score = finite(current.get("score"))
    forecasts = cycle_row.get("forecasts") if isinstance(cycle_row.get("forecasts"), dict) else {}
    forecast_3m = _feed_stage(
        forecasts.get("3m"), policy, "3m", current_score,
        industry=industry, kr=kr, gl=gl, korea_equity=korea_equity, boom=boom, direct=direct,
    )
    forecast_3_6m = _feed_stage(
        forecasts.get("3_6m"), policy, "3_6m", current_score,
        industry=industry, kr=kr, gl=gl, korea_equity=korea_equity, boom=boom, direct=direct,
    )
    forecast_6_12m = _feed_stage(
        forecasts.get("6_12m"), policy, "6_12m", current_score,
        industry=industry, kr=kr, gl=gl, korea_equity=korea_equity, boom=boom, direct=direct,
    )
    future_score = finite(forecast_3m.get("score"))

    # OOS 상태 기반 bridge 한도
    oos_status = str(prospective_summary.get("status", "PENDING"))
    evaluated_cases = int(prospective_summary.get("evaluated_cases", 0))
    bridge_limits = _oos_bridge_limits(oos_status, evaluated_cases, policy)

    # bridge 신호 계산 (OOS 한도 범위 내)
    if future_score is not None and current_score is not None:
        delta_sig = round(future_score - current_score, 1)
        overlay_signal = clamp(0.60 * ((future_score - 50.0) / 50.0) + 0.40 * clamp(delta_sig / 15.0, -1, 1), -1, 1)
        future_quality = float(forecast_3m.get("quality_score") or 0.0)
        min_overlay_q = float(policy.get("minimum_stock_overlay_quality", 60))
        stock_overlay_allowed = future_quality >= min_overlay_q and bridge_limits["allowed_auxiliary"]
        overlay_points = bridge_limits["max_points"] * overlay_signal * (future_quality / 100.0) if stock_overlay_allowed else 0.0
    else:
        overlay_signal = 0.0
        overlay_points = 0.0
        stock_overlay_allowed = False

    model = {
        "current": "industry_observed_metrics_only",
        "future": "industry_leading_metrics_plus_sensitive_macro_estimated" if forecast_3m.get("score_source") == "estimated" else "industry_leading_metrics_plus_sensitive_macro",
        "data_status": "scored" if current.get("status") == "scored" else "insufficient_data",
        "regime": (regime.get("primary_regime") if regime else None) or "neutral",
        "regime_label": (regime.get("primary_label") if regime else None) or "중립",
        "regime_active": [r["name"] for r in (regime.get("active_regimes") or [])],
        "current_inputs": industry.get("current_metric_groups") or [],
        "leading_inputs": industry.get("leading_metric_groups") or [],
        "specialized_current_metrics": industry.get("specialized_current_metrics") or [],
        "specialized_leading_metrics": industry.get("specialized_leading_metrics") or [],
        "feed_generated_at_utc": cycle_row.get("generated_at_utc"),
    }
    return {
        "industry_key": industry["key"], "industry_label": industry["label"], "aliases": industry.get("aliases") or [],
        "current": current,
        "forecast_3m": forecast_3m,
        "forecast_3_6m": forecast_3_6m,
        "forecast_6_12m": forecast_6_12m,
        "direct_market": direct,
        "theme_bridge": _theme_signal(industry, boom, policy),
        "stock_prediction_bridge": {
            "allowed_as_auxiliary": bool(stock_overlay_allowed),
            "allowed_as_primary": bool(bridge_limits["allowed_primary"] and stock_overlay_allowed),
            "bounded_direction_adjustment_points": round(overlay_points, 2),
            "max_abs_adjustment_points": bridge_limits["max_points"],
            "signal_normalized": round(overlay_signal, 4),
            "quality_score": float(forecast_3m.get("quality_score") or 0.0),
            "validation_status": oos_status,
            "oos_current_limit": bridge_limits["max_points"],
            "oos_evaluated_cases": evaluated_cases,
            "rule": f"OOS {oos_status} (평가 {evaluated_cases}건): 허용한도 ±{bridge_limits['max_points']}pt. OOS 통과 후 단계적 확대.",
        },
        "interpretation": {
            "headline": f"현재 {current.get('band', '데이터 부족')} {current_score:.0f}/100" if current_score is not None else "산업실물지표 연결 대기",
            "beginner": (
                "산업 실물지표 피드의 총점과 품질·커버리지를 사용했습니다. 향후 전망은 산업 선행지표에 산업별 민감도에 맞춘 글로벌·한국 경기 변수를 추가합니다."
                if current_score is not None else "산업 실물지표 품질 기준을 충족하지 못해 점수를 표시하지 않습니다."
            ),
            "warning": "산업환경 점수는 기업 자체의 주가나 재무가치 점수가 아닙니다.",
        },
        "quality": {
            "sector_specificity_score": 100.0 if current_score is not None else 0.0,
            "forecast_upstream_quality_score": float(forecast_3m.get("quality_score") or 0.0),
            "source_freshness_score": 100.0 if cycle_row.get("generated_at_utc") else 0.0,
            "prospective_validation": prospective_summary,
            "direct_sector_market_available": finite(direct.get("market_internal_score")) is not None,
            "industry_specific_valuation_available": finite(direct.get("valuation_score")) is not None,
            "industry_historical_valuation_available": direct.get("valuation_history_ready") is True,
            "data_status": model["data_status"],
            "current_metric_coverage": current.get("data_coverage_pct", 0.0),
            "leading_metric_coverage": forecast_3m.get("data_coverage_pct", 0.0),
            "oos_bridge_status": oos_status,
            "oos_evaluated_cases": evaluated_cases,
            "oos_bridge_max_points": bridge_limits["max_points"],
        },
        "score_model": model,
        "notes": industry.get("notes") or "",
    }


def _extract_nowcast_metric(nowcast_data: dict[str, Any] | None, industry_key: str) -> dict[str, Any] | None:
    """customs_nowcast_raw.json에서 해당 산업의 nowcast metric 추출."""
    if not nowcast_data or not isinstance(nowcast_data, dict):
        return None
    if nowcast_data.get("status") not in {"raw", "scored"}:
        return None
    for ind in (nowcast_data.get("industries") or []):
        if isinstance(ind, dict) and str(ind.get("industry_key", "")) == industry_key:
            metrics = (ind.get("current") or {}).get("metrics") or []
            return metrics[0] if metrics else None
    return None


def _extract_dart_earnings_metric(dart_data: dict[str, Any] | None, industry_key: str) -> dict[str, Any] | None:
    """dart_earnings_raw.json에서 해당 산업의 earnings metric 추출."""
    if not dart_data or not isinstance(dart_data, dict):
        return None
    if dart_data.get("status") not in {"raw", "scored"}:
        return None
    for ind in (dart_data.get("industries") or []):
        if isinstance(ind, dict) and str(ind.get("industry_key", "")) == industry_key:
            metrics = (ind.get("current") or {}).get("metrics") or []
            return metrics[0] if metrics else None
    return None


def score_industry(
    industry: dict[str, Any],
    policy: dict[str, Any],
    korea_rate: dict[str, Any],
    korea_equity: dict[str, Any],
    global_bundle: dict[str, Any],
    boom: dict[str, Any],
    industry_cycle: dict[str, Any],
    direct_market: dict[str, Any],
    freshness_quality: float,
    prospective_summary: dict[str, Any],
    nowcast_data: dict[str, Any] | None = None,
    dart_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kr = _extract_korea(korea_rate, korea_equity)
    gl = _extract_global(global_bundle)
    direct = (direct_market.get("industries") or {}).get(industry["key"]) or {}
    cycle_row = _industry_cycle_row(industry_cycle, industry["key"])
    nowcast_metric = _extract_nowcast_metric(nowcast_data, industry["key"])
    dart_metric = _extract_dart_earnings_metric(dart_data, industry["key"])
    # ── 매크로 레짐 감지 (per-industry 가중치 조정용) ─────────────────────────
    _regime = detect_regime(gl, kr)

    # 피드가 명시적으로 pending 상태(status="pending" 또는 industries 비어있음)이면
    # estimated도 산출하지 않는다 — 수집기 미연결 명시 신호.
    feed_pending = (
        isinstance(industry_cycle, dict)
        and (
            industry_cycle.get("status") == "pending"
            or not industry_cycle.get("industries")
        )
    )

    if not cycle_row or not isinstance(cycle_row.get("current"), dict) or not finite(cycle_row.get("current", {}).get("score")):
        # feed_pending이면 null 유지, 연결됐으나 해당 산업 없으면 estimated 시도
        allow_estimated = not feed_pending
        return _pending_industry_result(
            industry, direct,
            "산업별 생산·출하·재고·주문·가격·마진 실측 피드가 연결되지 않았습니다.",
            prospective_summary,
            boom=boom, policy=policy,
            kr=kr if allow_estimated else None,
            gl=gl if allow_estimated else None,
            korea_equity=korea_equity if allow_estimated else None,
            nowcast_metric=nowcast_metric if allow_estimated else None,
            regime=_regime if allow_estimated else None,
            dart_metric=dart_metric if allow_estimated else None,
        )

    return _scored_industry_result_from_feed(
        industry, cycle_row, direct, policy, prospective_summary,
        boom=boom, kr=kr, gl=gl, korea_equity=korea_equity,
        regime=_regime, dart_metric=dart_metric,
    )
