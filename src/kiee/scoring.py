from __future__ import annotations

import math
from statistics import mean
from typing import Any

from .util import clamp, finite, find_month_row, nested, roundn, weighted_average

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
        # V70 theme is often narrower than the classical industry. A per-industry relevance
        # guard prevents a trendy subtheme from standing in for the whole sector cycle.
        current = clamp(50.0 + (current_raw - 50.0) * shrink * relevance, 0.0, 100.0)
        leading = clamp(50.0 + (lead_raw - 50.0) * shrink * relevance, 0.0, 100.0)
        component_values = {
            "industrial": industrial,
            "commercialization": commercialization,
            "investment": investment,
            "diffusion": diffusion,
            "boom": boom_score,
        }
        for component_key, component_value in component_values.items():
            adjusted = clamp(50.0 + (component_value - 50.0) * shrink * relevance, 0.0, 100.0)
            current_components[component_key].append(adjusted)
        leading_values = {
            "industrial": predicted,
            "commercialization": commercialization,
            "investment": investment,
            "diffusion": diffusion,
            "boom": boom_score,
        }
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
    # pieces = (score, blend weight, quality)
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

    # The current sector score is intentionally industry-only.  Broad Korea/global
    # macro series are forecast overlays and must not leak into the current reading.
    # The available industry-native inputs here are the sector theme/leading signal,
    # the direct KRX sector basket, and sector valuation.  Missing native inputs stay
    # missing so the aggregate quality/shrinkage exposes the limitation instead of
    # silently substituting a macro proxy.
    if not future:
        current_components = theme.get("current_components") or {}
        earnings = _combine_factor(
            [(finite(current_components.get("industrial")), 0.60, theme_q), (finite(current_components.get("commercialization")), 0.40, theme_q)],
            "산업 고유 실적·상업화 신호",
            "현재 산업점수에는 해당 산업의 실물·상업화 신호만 사용하며 한국경제·글로벌경제·환율·금리·광범위 주가지수 대용치는 제외합니다.",
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
        financial = _factor(
            None,
            0.0,
            "산업 고유 금융환경 지표 연결 대기",
            "한국 금리·환율·유동성·신용은 현재 산업점수에서 제외하고 향후 민감도 오버레이에만 사용합니다.",
        )
        direct_score = finite(direct.get("market_internal_score"))
        direct_q = finite(direct.get("market_internal_quality"), 0.0) or 0.0
        market_internal = _combine_factor(
            [(direct_score, 1.0, direct_q)],
            "해당 산업 KRX 대표바스켓 내부환경",
            "현재 산업점수에는 해당 산업 대표바스켓의 수익률·상승 breadth·수급만 반영하고 한국·글로벌 전체 주가지수는 제외합니다.",
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
            "financial_detail": {},
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
    # card10 > 50 = greater cost/supply pressure. Positive sensitivity means lower pressure is favorable.
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
        # Reserve 0.15 for the industry's own leading/theme signal. The
        # remaining weights move toward global equity only when sensitivity is high.
        global_equity_weight = 0.10 + 0.35 * global_equity_sens
        direct_weight = 0.60 - 0.25 * global_equity_sens
        broad_weight = 0.85 - global_equity_weight - direct_weight
        market_pieces = [(direct_score, direct_weight, direct_q * 0.70), (broad_market, broad_weight, 80.0), (global_equity, global_equity_weight, global_eq_q), (theme_score, 0.15 if theme.get("available") else 0.0, theme_q)]
    else:
        market_pieces = [(direct_score, 0.65, direct_q), (broad_market, 0.25, 90.0), (global_equity, 0.10, 90.0)]
    market_internal = _combine_factor(
        market_pieces,
        "KRX 대표바스켓 + 한국증시 직접환경 + 글로벌 주식환경",
        "KRX는 시장 전체 표를 소수 회 호출한 뒤 업종 대표바스켓만 로컬 필터링합니다. 미래값은 현재 수급을 과도하게 연장하지 않고 검증형 글로벌 3개월 주식환경 비중을 높입니다.",
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
            f"산업 자체 역사표본이 아직 {history_samples}개라 초기구간입니다. 전체시장 대비 PER/PBR은 산업구조 차이를 과대평가하지 않도록 50점 방향으로 축소하고 품질을 제한합니다."
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
    # Avoid false precision when quality-weighted coverage is low.
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


def _quality_score(aggregate: dict[str, Any], specificity: float, freshness: float, future: bool, forecast_upstream_quality: float, prospective_quality: float) -> float:
    qcov = float(aggregate.get("quality_weighted_coverage_pct") or 0.0)
    if future:
        return clamp(0.40 * qcov + 0.22 * specificity + 0.20 * forecast_upstream_quality + 0.10 * freshness + 0.08 * prospective_quality, 0, 100)
    return clamp(0.58 * qcov + 0.27 * specificity + 0.15 * freshness, 0, 100)


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
    factors = {
        key: _factor(None, 0.0, "산업실물지표 연결 대기", reason)
        for key in FACTOR_ORDER
    }
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


def _pending_industry_result(industry: dict[str, Any], direct: dict[str, Any], reason: str) -> dict[str, Any]:
    current = _pending_stage(reason)
    future = _pending_stage(reason)
    future["delta_points"] = None
    future["change_strength"] = "대기"
    future["direction"] = "대기"
    future["top_positive_reasons"] = []
    future["top_negative_reasons"] = []
    horizon_pending = {
        "score": None,
        "direction": "대기",
        "status": "insufficient_data",
        "reason": "해당 기간의 산업 선행지표가 연결되지 않아 전망을 계산하지 않았습니다.",
        "quality_score": 0.0,
        "data_coverage_pct": 0.0,
        "factors": {},
    }
    model = {
        "current": "industry_observed_metrics_only",
        "future": "industry_leading_metrics_plus_sensitive_macro",
        "data_status": "insufficient_data",
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
        "forecast_3m": future,
        "forecast_3_6m": horizon_pending,
        "forecast_6_12m": horizon_pending,
        "direct_market": direct,
        "theme_bridge": {"available": False, "current": None, "leading_3m": None, "quality": 0.0, "themes": [], "relevance_weight": 0.0},
        "stock_prediction_bridge": {
            "allowed_as_auxiliary": False,
            "allowed_as_primary": False,
            "bounded_direction_adjustment_points": 0.0,
            "max_abs_adjustment_points": 0.0,
            "signal_normalized": 0.0,
            "quality_score": 0.0,
            "validation_status": "PENDING",
            "rule": "산업실물지표가 연결되기 전에는 종목 신호로 산업경기를 대체하지 않음",
        },
        "interpretation": {
            "headline": "산업실물지표 연결 대기",
            "beginner": reason,
            "warning": "생산·출하·재고·주문·가격·마진 등 산업 고유 데이터가 부족해 점수를 표시하지 않습니다.",
        },
        "quality": {
            "sector_specificity_score": 0.0,
            "forecast_upstream_quality_score": 0.0,
            "source_freshness_score": 0.0,
            "data_status": "insufficient_data",
            "current_metric_coverage": 0.0,
            "leading_metric_coverage": 0.0,
        },
        "score_model": model,
        "notes": industry.get("notes") or "",
    }


def _feed_stage(stage: dict[str, Any] | None, policy: dict[str, Any], horizon: str, current_score: float | None = None) -> dict[str, Any]:
    """Render an authoritative industry-cycle feed stage without imputing values."""
    stage = stage if isinstance(stage, dict) else {}
    score = finite(stage.get("score"))
    quality = finite(stage.get("quality_score"))
    coverage = finite(stage.get("data_coverage_pct"))
    cycle_policy = policy.get("industry_cycle_scoring") or {}
    minimum = float(cycle_policy.get("minimum_current_coverage_pct", 60.0)) if horizon == "current" else float(cycle_policy.get("minimum_leading_coverage_pct", 60.0))
    if score is None or quality is None or coverage is None or quality <= 0 or coverage < minimum:
        return {
            "score": None,
            "direction": "대기" if horizon != "current" else None,
            "status": "insufficient_data",
            "reason": f"{horizon} 산업실물지표 품질·커버리지 기준을 충족하지 못해 점수를 표시하지 않습니다.",
            "quality_score": round(max(0.0, quality or 0.0), 1),
            "data_coverage_pct": round(max(0.0, coverage or 0.0), 1),
            "factors": {},
            "metrics": stage.get("metrics") or [],
        }
    score = clamp(score, 0.0, 100.0)
    factor_scores = stage.get("factor_scores") if isinstance(stage.get("factor_scores"), dict) else {}
    factors = {}
    for key in FACTOR_ORDER:
        factor_value = finite(factor_scores.get(key))
        factors[key] = _factor(
            factor_value,
            quality if factor_value is not None else 0.0,
            "산업실물지표 피드" if factor_value is not None else "산업실물지표 피드 팩터분해 대기",
            "원천 산업 피드의 팩터 점수" if factor_value is not None else "총점은 연결됐지만 세부 팩터 분해값은 제공되지 않았습니다.",
        )
    delta = round(score - current_score, 1) if current_score is not None and horizon != "current" else None
    direction = str(stage.get("direction") or ("개선" if delta is not None and delta > 1 else "악화" if delta is not None and delta < -1 else "유지" if delta is not None else "대기"))
    return {
        "score": round(score, 1),
        "band": _score_band(score, policy),
        "cycle_phase": stage.get("cycle_phase"),
        "quality_score": round(clamp(quality, 0.0, 100.0), 1),
        "data_coverage_pct": round(clamp(coverage, 0.0, 100.0), 1),
        "quality_weighted_coverage_pct": round(clamp(coverage * quality / 100.0, 0.0, 100.0), 1),
        "factors": factors,
        "contributions": [],
        "metrics": stage.get("metrics") or [],
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
    }


def _scored_industry_result_from_feed(industry: dict[str, Any], cycle_row: dict[str, Any], direct: dict[str, Any], policy: dict[str, Any], prospective_summary: dict[str, Any]) -> dict[str, Any]:
    current = _feed_stage(cycle_row.get("current"), policy, "current")
    current_score = finite(current.get("score"))
    forecasts = cycle_row.get("forecasts") if isinstance(cycle_row.get("forecasts"), dict) else {}
    forecast_3m = _feed_stage(forecasts.get("3m"), policy, "3m", current_score)
    forecast_3_6m = _feed_stage(forecasts.get("3_6m"), policy, "3_6m", current_score)
    forecast_6_12m = _feed_stage(forecasts.get("6_12m"), policy, "6_12m", current_score)
    future_score = finite(forecast_3m.get("score"))
    positive = [{"factor": "industry_feed", "label": str(value), "points": None, "score": future_score} for value in (forecast_3m.get("positive_indicators") or [])]
    negative = [{"factor": "industry_feed", "label": str(value), "points": None, "score": future_score} for value in (forecast_3m.get("negative_indicators") or [])]
    model = {
        "current": "industry_observed_metrics_only",
        "future": "industry_leading_metrics_plus_sensitive_macro",
        "data_status": "scored" if current.get("status") == "scored" else "insufficient_data",
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
        "theme_bridge": {"available": False, "current": None, "leading_3m": None, "quality": 0.0, "themes": [], "relevance_weight": 0.0},
        "stock_prediction_bridge": {
            "allowed_as_auxiliary": False, "allowed_as_primary": False,
            "bounded_direction_adjustment_points": 0.0, "max_abs_adjustment_points": 0.0,
            "signal_normalized": 0.0, "quality_score": float(forecast_3m.get("quality_score") or 0.0),
            "validation_status": prospective_summary.get("status", "PENDING"),
            "rule": "산업실물지표 피드가 산업점수의 원천이며 개별종목 신호로 대체하지 않습니다.",
        },
        "interpretation": {
            "headline": f"현재 {current.get('band', '데이터 부족')} {current_score:.0f}/100" if current_score is not None else "산업실물지표 연결 대기",
            "beginner": "산업 실물지표 피드의 총점과 품질·커버리지를 사용했습니다. 향후 전망은 산업 선행지표에 산업별 민감도에 맞춘 글로벌·한국 경기 변수를 추가합니다." if current_score is not None else "산업 실물지표 품질 기준을 충족하지 못해 점수를 표시하지 않습니다.",
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
        },
        "score_model": model,
        "notes": industry.get("notes") or "",
    }


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
) -> dict[str, Any]:
    kr = _extract_korea(korea_rate, korea_equity)
    gl = _extract_global(global_bundle)
    direct = (direct_market.get("industries") or {}).get(industry["key"]) or {}
    cycle_row = _industry_cycle_row(industry_cycle, industry["key"])
    if not cycle_row or not isinstance(cycle_row.get("current"), dict) or not finite(cycle_row.get("current", {}).get("score")):
        return _pending_industry_result(
            industry,
            direct,
            "산업별 생산·출하·재고·주문·가격·마진 실측 피드가 연결되지 않았습니다. 입력이 연결될 때까지 임의의 ETF·테마·거시 대체점수를 사용하지 않습니다.",
        )
    return _scored_industry_result_from_feed(industry, cycle_row, direct, policy, prospective_summary)
    current_factors, current_meta = _build_factors(industry, policy, kr, gl, korea_equity, boom, direct, False)
    future_factors, future_meta = _build_factors(industry, policy, kr, gl, korea_equity, boom, direct, True)
    current_agg = _aggregate_score(current_factors, industry.get("weights_current") or {})
    future_agg = _aggregate_score(future_factors, industry.get("weights_3m") or {})
    specificity = _sector_specificity(current_meta["theme"], direct)
    forecast_upstream_quality = mean([kr.get("rate_quality_3m", 60.0), kr.get("fx_quality_3m", 60.0), kr.get("liquidity_quality_3m", 60.0), kr.get("strength_quality_3m", 60.0), gl.get("consumer_3m_quality", 55.0), gl.get("cost_3m_quality", 55.0), gl.get("equity_3m_quality", 55.0)])
    prospective_quality = float(prospective_summary.get("quality_score") or 30.0)
    current_quality = _quality_score(current_agg, specificity, freshness_quality, False, forecast_upstream_quality, prospective_quality)
    future_quality = _quality_score(future_agg, specificity, freshness_quality, True, forecast_upstream_quality, prospective_quality)
    current_score = float(current_agg["score"])
    future_score = float(future_agg["score"])
    delta = future_score - current_score
    current_band = _score_band(current_score, policy)
    future_band = _score_band(future_score, policy)
    delta_strength = _delta_strength(delta, policy)
    direction = "개선" if delta > 1.0 else ("악화" if delta < -1.0 else "유지")
    validated = prospective_summary.get("status") == "PASSED"
    max_points = float(policy.get("stock_overlay_max_points_validated" if validated else "stock_overlay_max_points_pending_oos", 5.0))
    overlay_signal = clamp(0.60 * ((future_score - 50.0) / 50.0) + 0.40 * clamp(delta / 15.0, -1, 1), -1, 1)
    stock_overlay_allowed = future_quality >= float(policy.get("minimum_stock_overlay_quality", 60))
    overlay_points = max_points * overlay_signal * (future_quality / 100.0) if stock_overlay_allowed else 0.0
    positive = _top_reasons(future_agg["contributions"], True)
    negative = _top_reasons(future_agg["contributions"], False)
    sensitivities = industry.get("sensitivities") or {}
    return {
        "industry_key": industry["key"], "industry_label": industry["label"], "aliases": industry.get("aliases") or [],
        "current": {
            "score": round(current_score, 1), "band": current_band, "quality_score": round(current_quality, 1),
            "data_coverage_pct": round(current_agg["base_data_coverage_pct"], 1),
            "quality_weighted_coverage_pct": round(current_agg["quality_weighted_coverage_pct"], 1),
            "factors": current_factors, "contributions": current_agg["contributions"],
        },
        "forecast_3m": {
            "score": round(future_score, 1), "band": future_band, "delta_points": round(delta, 1),
            "direction": direction, "change_strength": delta_strength, "quality_score": round(future_quality, 1),
            "data_coverage_pct": round(future_agg["base_data_coverage_pct"], 1),
            "quality_weighted_coverage_pct": round(future_agg["quality_weighted_coverage_pct"], 1),
            "factors": future_factors, "contributions": future_agg["contributions"],
            "top_positive_reasons": positive, "top_negative_reasons": negative,
        },
        "direct_market": direct,
        "theme_bridge": future_meta["theme"],
        "stock_prediction_bridge": {
            "allowed_as_auxiliary": bool(stock_overlay_allowed),
            "allowed_as_primary": bool(validated and policy.get("forecast_primary_use_allowed") is True),
            "bounded_direction_adjustment_points": round(overlay_points, 2),
            "max_abs_adjustment_points": max_points,
            "signal_normalized": round(overlay_signal, 4),
            "quality_score": round(future_quality, 1),
            "validation_status": prospective_summary.get("status", "PENDING"),
            "rule": "산업전망은 개별종목 방향예측의 보조 오버레이로만 사용하며, 자체 OOS가 통과하기 전 최대 영향도를 제한합니다.",
        },
        "interpretation": {
            "headline": f"현재 {current_band} {current_score:.0f}/100 · 3개월 {future_band} {future_score:.0f}/100 · {delta:+.1f}점 {delta_strength} {direction}",
            "beginner": (
                f"현재 {industry['label']} 산업환경은 {current_band} 구간입니다. 3개월 예상은 {future_score:.0f}점으로 현재보다 {abs(delta):.1f}점 "
                + ("높아져 개선 방향입니다." if delta > 1 else ("낮아져 불리해질 가능성을 경계합니다." if delta < -1 else "차이가 작아 투자 스탠스를 크게 바꿀 수준은 아닙니다."))
            ),
            "warning": "점수는 산업환경의 상대적 유불리이며 해당 산업 모든 종목의 주가 상승·하락을 보장하지 않습니다.",
        },
        "quality": {
            "sector_specificity_score": round(specificity, 1),
            "forecast_upstream_quality_score": round(forecast_upstream_quality, 1),
            "source_freshness_score": round(freshness_quality, 1),
            "prospective_validation": prospective_summary,
            "direct_sector_market_available": finite(direct.get("market_internal_score")) is not None,
            "industry_specific_valuation_available": finite(direct.get("valuation_score")) is not None,
            "industry_historical_valuation_available": direct.get("valuation_history_ready") is True,
            "industry_valuation_history_samples": int(direct.get("valuation_history_samples") or 0),
            "industry_valuation_method": direct.get("valuation_method"),
        },
        "score_model": {
            "current": "industry_only",
            "future": "industry_plus_sensitive_macro",
            "current_inputs": [
                "산업 고유 실물·상업화·투자·확산 신호",
                "해당 산업 KRX 대표바스켓 내부환경",
                "해당 산업 대표바스켓 PER·PBR",
            ],
            "current_excluded": [
                "한국 금리·환율·유동성·신용",
                "한국 종합주가지수",
                "글로벌 경기·글로벌 주가지수",
            ],
            "future_macro_sensitivities": {
                key: round(float(value), 4)
                for key, value in sensitivities.items()
                if finite(value) is not None and abs(float(value)) > 0
            },
            "future_rule": "현재 산업 고유 신호를 기준으로 한국·글로벌 거시지표를 산업별 민감도에 따라 향후 3개월 점수에 반영",
        },
        "notes": industry.get("notes") or "",
    }
