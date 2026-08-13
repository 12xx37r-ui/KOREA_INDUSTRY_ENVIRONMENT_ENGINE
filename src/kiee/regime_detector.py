"""
regime_detector.py
매크로 레짐 감지 및 팩터 가중치 동적 조정.

레짐 분류:
  tightening  — 금리 인상기 / 미 장기금리 고공
  easing      — 금리 인하기 / 유동성 개선
  high_fx     — 고환율기 (원화 약세)
  risk_off    — 리스크 오프 (글로벌 증시 하강 + 신용 스프레드)
  neutral     — 명확한 신호 없음

복수 레짐이 동시에 감지되면 strength 가중 평균으로 blending.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .util import clamp, finite, read_json

FACTOR_ORDER = [
    "earnings_momentum", "demand_cycle", "pricing_margin",
    "financial_conditions", "market_internals", "valuation",
]

# ── 레짐 감지 임계값 ──────────────────────────────────────────────────────────
_T = {
    "korea_rate_hike_threshold":   0.10,   # 3m 기준금리 상승 기대치 (pct)
    "korea_rate_cut_threshold":   -0.10,   # 3m 기준금리 인하 기대치 (pct)
    "us10y_high":                  4.50,   # 미 10년물 고공 기준 (%)
    "us10y_low":                   3.50,   # 미 10년물 완화 기준 (%)
    "usdkrw_high":              1350.0,    # 고환율 기준
    "krw_strength_weak":          38.0,    # 원화 강도 하한 (0~100)
    "global_equity_low":          38.0,    # 글로벌 증시 약세 기준
    "credit_health_tight":        -0.25,   # 신용환경 경색 기준
    "liquidity_positive":          0.15,   # 유동성 개선 기준
}


def detect_regime(
    gl: dict[str, Any],
    kr: dict[str, Any],
    profiles_path: Path | None = None,
) -> dict[str, Any]:
    """
    매크로 데이터에서 현재 레짐을 감지하고
    팩터 multiplier를 반환한다.

    Parameters
    ----------
    gl  : _extract_global() 반환값
    kr  : _extract_korea() 반환값
    profiles_path : regime_weight_profiles.json 경로 (없으면 내장 기본값 사용)

    Returns
    -------
    {
      "primary_regime": str,
      "active_regimes": [{"name": str, "strength": float, "label": str}],
      "multipliers": {factor: float},
      "quality_scale": float,
      "diagnostics": [str],
    }
    """
    profiles = _load_profiles(profiles_path)
    diag: list[str] = []

    # ── 입력 신호 추출 ────────────────────────────────────────────────────────
    rate_cur  = finite(kr.get("rate_current"), 2.75) or 2.75
    rate_3m   = finite(kr.get("rate_3m"), rate_cur) or rate_cur
    rate_delta = rate_3m - rate_cur

    us10y = finite(gl.get("us10y"), 4.0) or 4.0
    usdkrw = finite(kr.get("fx_current"), 1300.0) or 1300.0
    krw_strength = finite(kr.get("strength_current"), 50.0) or 50.0
    liquidity = finite(kr.get("liquidity_current"), 0.0) or 0.0
    credit = finite(kr.get("credit_health"), 0.0) or 0.0
    global_equity = finite(gl.get("equity_current"), 50.0) or 50.0

    diag.append(f"rate_cur={rate_cur:.2f} rate_3m={rate_3m:.2f} delta={rate_delta:+.3f}")
    diag.append(f"us10y={us10y:.2f} usdkrw={usdkrw:.0f} krw_strength={krw_strength:.1f}")
    diag.append(f"global_equity={global_equity:.1f} credit={credit:.3f} liquidity={liquidity:.3f}")

    # ── 레짐별 강도(strength) 계산 ────────────────────────────────────────────
    active: list[dict[str, Any]] = []

    # tightening
    t_strength = 0.0
    if rate_delta >= _T["korea_rate_hike_threshold"]:
        t_strength = clamp(rate_delta / 0.50, 0.25, 1.0)
    if us10y >= _T["us10y_high"]:
        t_strength = max(t_strength, clamp((us10y - _T["us10y_high"]) / 1.0 + 0.4, 0.25, 1.0))
    if t_strength >= 0.25:
        active.append({"name": "tightening", "strength": round(t_strength, 3)})
        diag.append(f"tightening strength={t_strength:.3f}")

    # easing
    e_strength = 0.0
    if rate_delta <= _T["korea_rate_cut_threshold"]:
        e_strength = clamp(abs(rate_delta) / 0.50, 0.25, 1.0)
    if us10y <= _T["us10y_low"] and liquidity >= _T["liquidity_positive"]:
        e_strength = max(e_strength, clamp((_T["us10y_low"] - us10y) / 0.80 + 0.3, 0.25, 1.0))
    if e_strength >= 0.25:
        active.append({"name": "easing", "strength": round(e_strength, 3)})
        diag.append(f"easing strength={e_strength:.3f}")

    # high_fx
    fx_strength = 0.0
    if usdkrw >= _T["usdkrw_high"]:
        fx_strength = clamp((usdkrw - _T["usdkrw_high"]) / 150.0 + 0.3, 0.25, 1.0)
        if krw_strength <= _T["krw_strength_weak"]:
            fx_strength = min(fx_strength + 0.2, 1.0)
    if fx_strength >= 0.25:
        active.append({"name": "high_fx", "strength": round(fx_strength, 3)})
        diag.append(f"high_fx strength={fx_strength:.3f}")

    # risk_off
    ro_strength = 0.0
    if global_equity <= _T["global_equity_low"] and credit <= _T["credit_health_tight"]:
        ro_strength = clamp(
            (_T["global_equity_low"] - global_equity) / 25.0 * 0.6
            + abs(credit + _T["credit_health_tight"]) / 0.5 * 0.4,
            0.25, 1.0,
        )
    elif global_equity <= _T["global_equity_low"] - 5:
        ro_strength = 0.35
    if ro_strength >= 0.25:
        active.append({"name": "risk_off", "strength": round(ro_strength, 3)})
        diag.append(f"risk_off strength={ro_strength:.3f}")

    # 최대 2개 레짐만 활성화 (strength 상위)
    active.sort(key=lambda x: x["strength"], reverse=True)
    active = active[:2]

    if not active:
        active = [{"name": "neutral", "strength": 1.0}]
        diag.append("regime=neutral (no signal)")

    # ── multiplier blending ────────────────────────────────────────────────────
    total_strength = sum(a["strength"] for a in active)
    blended: dict[str, float] = {f: 0.0 for f in FACTOR_ORDER}
    blended_quality_scale = 0.0

    for reg in active:
        weight = reg["strength"] / max(total_strength, 1e-9)
        profile = (profiles.get("regimes") or {}).get(reg["name"]) or {}
        mults = profile.get("multipliers") or {f: 1.0 for f in FACTOR_ORDER}
        q_scale = float(profile.get("quality_scale", 1.0))
        for f in FACTOR_ORDER:
            blended[f] += float(mults.get(f, 1.0)) * weight
        blended_quality_scale += q_scale * weight

    # primary regime = highest strength
    primary = active[0]["name"]

    # label 추가
    for a in active:
        prof = (profiles.get("regimes") or {}).get(a["name"]) or {}
        a["label"] = prof.get("label", a["name"])

    return {
        "primary_regime": primary,
        "primary_label": active[0].get("label", primary),
        "active_regimes": active,
        "multipliers": {f: round(blended[f], 4) for f in FACTOR_ORDER},
        "quality_scale": round(blended_quality_scale, 4),
        "diagnostics": diag,
    }


def apply_regime_weights(
    base_weights: dict[str, float],
    regime: dict[str, Any],
) -> dict[str, float]:
    """
    base_weights에 레짐 multiplier를 곱한 뒤 재정규화.
    산업별 커스텀 weights_current/3m를 보존하면서 레짐 방향만 반영.
    """
    mults = regime.get("multipliers") or {f: 1.0 for f in FACTOR_ORDER}
    raw: dict[str, float] = {}
    for f in FACTOR_ORDER:
        base = float(base_weights.get(f, 0.0))
        mult = float(mults.get(f, 1.0))
        raw[f] = base * mult

    total = sum(raw.values())
    if total <= 0:
        return dict(base_weights)
    # 재정규화 — 총합 = 원래 base_weights 총합 유지
    base_total = sum(float(v) for v in base_weights.values())
    scale = base_total / total if total > 0 else 1.0
    return {f: round(v * scale, 6) for f, v in raw.items()}


def _load_profiles(path: Path | None) -> dict[str, Any]:
    if path and path.exists():
        return read_json(path, {}) or {}
    # 내장 기본값 (파일 없어도 동작)
    return {
        "regimes": {
            "tightening": {"label": "금리 인상기", "multipliers": {"earnings_momentum": 1.20, "demand_cycle": 1.00, "pricing_margin": 0.90, "financial_conditions": 1.40, "market_internals": 0.85, "valuation": 1.25}, "quality_scale": 1.0},
            "easing":     {"label": "금리 인하기", "multipliers": {"earnings_momentum": 1.10, "demand_cycle": 1.30, "pricing_margin": 1.10, "financial_conditions": 0.80, "market_internals": 1.20, "valuation": 0.90}, "quality_scale": 1.0},
            "high_fx":    {"label": "고환율기",    "multipliers": {"earnings_momentum": 1.15, "demand_cycle": 0.95, "pricing_margin": 1.20, "financial_conditions": 1.10, "market_internals": 1.00, "valuation": 0.95}, "quality_scale": 1.0},
            "risk_off":   {"label": "리스크 오프", "multipliers": {"earnings_momentum": 0.85, "demand_cycle": 0.80, "pricing_margin": 0.90, "financial_conditions": 1.30, "market_internals": 1.40, "valuation": 1.20}, "quality_scale": 0.95},
            "neutral":    {"label": "중립",        "multipliers": {"earnings_momentum": 1.0, "demand_cycle": 1.0, "pricing_margin": 1.0, "financial_conditions": 1.0, "market_internals": 1.0, "valuation": 1.0},  "quality_scale": 1.0},
        }
    }
