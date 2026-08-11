from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

from .util import age_hours, clamp, finite, read_json, roundn, utc_now_iso, write_json

KST = timezone(timedelta(hours=9))


def _frame_empty(frame: Any) -> bool:
    return frame is None or bool(getattr(frame, "empty", False))


def _ticker(value: Any) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6)[-6:] if digits else ""


def _row_value(row: Any, keys: tuple[str, ...]) -> float | None:
    if hasattr(row, "get"):
        for key in keys:
            value = finite(row.get(key))
            if value is not None:
                return value
    return None


def _rows(frame: Any) -> dict[str, dict[str, float | None]]:
    out: dict[str, dict[str, float | None]] = {}
    if _frame_empty(frame) or not hasattr(frame, "iterrows"):
        return out
    for idx, row in frame.iterrows():
        code = _ticker(idx)
        if not code:
            continue
        out[code] = {
            "close": _row_value(row, ("종가", "Close", "현재가")),
            "change_pct": _row_value(row, ("등락률", "등락률(%)", "변동률", "수익률")),
            "per": _row_value(row, ("PER", "PER(배)")),
            "pbr": _row_value(row, ("PBR", "PBR(배)")),
            "net_value": _row_value(row, ("순매수거래대금", "순매수", "순매수금액", "거래대금")),
        }
    return out


def _merge(target: dict[str, dict[str, float | None]], source: dict[str, dict[str, float | None]]) -> None:
    for code, values in source.items():
        row = target.setdefault(code, {})
        for key, value in values.items():
            if value is not None:
                row[key] = value


def _theme_kr_tickers(industry: dict[str, Any], boom_snapshot: dict[str, Any]) -> list[str]:
    wanted = set(str(x) for x in (industry.get("theme_ids") or []))
    if not wanted:
        return []
    out: list[str] = []
    for row in boom_snapshot.get("decisions") or []:
        if not isinstance(row, dict) or str(row.get("theme_id")) not in wanted:
            continue
        for company in row.get("companies") or []:
            if not isinstance(company, dict):
                continue
            ticker = str(company.get("ticker") or "").upper().strip()
            if ticker.endswith(".KS") or ticker.endswith(".KQ"):
                code = _ticker(ticker.split(".", 1)[0])
                if code:
                    out.append(code)
    return out


def _business_window(days: int = 35) -> tuple[str, str]:
    now = datetime.now(KST).date()
    return (now - timedelta(days=days)).strftime("%Y%m%d"), now.strftime("%Y%m%d")


def _flow_frame(stock: Any, start: str, end: str, market: str, investor: str, diagnostics: list[str]) -> tuple[Any, int]:
    attempts = 0
    fn = getattr(stock, "get_market_net_purchases_of_equities_by_ticker", None)
    if callable(fn):
        attempts += 1
        try:
            return fn(start, end, market=market, investor=investor), attempts
        except Exception as exc:
            diagnostics.append(f"flow:{market}:{investor}:primary:{type(exc).__name__}:{str(exc)[:120]}")
    # Fallback is attempted only when the normal API is unavailable/failed.
    fn2 = getattr(stock, "get_market_trading_value_by_ticker", None)
    if callable(fn2):
        attempts += 1
        try:
            return fn2(start, end, market=market, investor=investor), attempts
        except Exception as exc:
            diagnostics.append(f"flow:{market}:{investor}:fallback:{type(exc).__name__}:{str(exc)[:120]}")
    return None, attempts


def _relative_valuation_score(sector_pers: list[float], sector_pbrs: list[float], broad_per: float | None, broad_pbr: float | None) -> float | None:
    pieces: list[tuple[float, float]] = []
    if sector_pers and broad_per and broad_per > 0:
        sp = median(sector_pers)
        if sp > 0:
            pieces.append((50.0 + 22.0 * math.log(broad_per / sp), 0.6))
    if sector_pbrs and broad_pbr and broad_pbr > 0:
        sb = median(sector_pbrs)
        if sb > 0:
            pieces.append((50.0 + 18.0 * math.log(broad_pbr / sb), 0.4))
    if not pieces:
        return None
    num = sum(clamp(score, 0.0, 100.0) * weight for score, weight in pieces)
    den = sum(weight for _, weight in pieces)
    return clamp(num / den, 0.0, 100.0)


def collect_sector_market(
    root: Path,
    industries: list[dict[str, Any]],
    boom_snapshot: dict[str, Any],
    stock_module: Any = None,
    allow_live: bool = True,
    max_lkg_age_hours: float = 120.0,
) -> dict[str, Any]:
    """Bulk KRX market-internal snapshot.

    Normal live budget: 2 price-change + 4 investor-flow + 2 fundamentals calls.
    All industries are filtered locally from those market-wide tables. No per-company calls.
    """
    cache_path = root / "input_cache" / "latest" / "krx_sector_market.json"
    previous = read_json(cache_path, {}) or {}
    diagnostics: list[str] = []
    calls = 0
    start, end = _business_window(35)

    stock = stock_module
    if stock is None and allow_live:
        try:
            from pykrx import stock as pykrx_stock  # type: ignore
            stock = pykrx_stock
        except Exception as exc:
            diagnostics.append(f"pykrx_import:{type(exc).__name__}:{exc}")

    if stock is None:
        previous_age = age_hours(previous.get("generated_at_utc")) if isinstance(previous, dict) else None
        reusable = bool(
            isinstance(previous, dict)
            and previous.get("industries")
            and previous_age is not None
            and previous_age <= max_lkg_age_hours
        )
        if reusable:
            reused = dict(previous)
            reused["stale"] = True
            reused["source_mode"] = "lkg-no-pykrx"
            reused["lkg_age_hours"] = roundn(previous_age, 2)
            reused["lkg_max_age_hours"] = max_lkg_age_hours
            reused["diagnostics"] = list(reused.get("diagnostics") or []) + diagnostics
            return reused
        if isinstance(previous, dict) and previous.get("industries"):
            diagnostics.append(
                "KRX LKG expired or timestamp unavailable: age=" + str(roundn(previous_age, 2))
                + "h max=" + str(max_lkg_age_hours) + "h"
            )
        return {
            "schema_version": "1.0.0", "generated_at_utc": utc_now_iso(), "available": False,
            "stale": False, "source_mode": "unavailable", "normal_live_calls": 0,
            "industries": {}, "diagnostics": diagnostics or ["pykrx unavailable"],
        }

    price_all: dict[str, dict[str, float | None]] = {}
    fundamental_all: dict[str, dict[str, float | None]] = {}
    flow_foreign: dict[str, dict[str, float | None]] = {}
    flow_institution: dict[str, dict[str, float | None]] = {}

    for market in ("KOSPI", "KOSDAQ"):
        try:
            frame = stock.get_market_price_change_by_ticker(start, end, market=market)
            calls += 1
            _merge(price_all, _rows(frame))
        except Exception as exc:
            diagnostics.append(f"price:{market}:{type(exc).__name__}:{str(exc)[:140]}")
        try:
            frame = stock.get_market_fundamental_by_ticker(end, market=market)
            calls += 1
            _merge(fundamental_all, _rows(frame))
        except Exception as exc:
            diagnostics.append(f"fundamental:{market}:{type(exc).__name__}:{str(exc)[:140]}")
        for investor, target in (("외국인", flow_foreign), ("기관합계", flow_institution)):
            frame, flow_calls = _flow_frame(stock, start, end, market, investor, diagnostics)
            calls += flow_calls
            _merge(target, _rows(frame))

    broad_pers = [float(v.get("per")) for v in fundamental_all.values() if finite(v.get("per")) and float(v.get("per")) > 0]
    broad_pbrs = [float(v.get("pbr")) for v in fundamental_all.values() if finite(v.get("pbr")) and float(v.get("pbr")) > 0]
    broad_per = median(broad_pers) if broad_pers else None
    broad_pbr = median(broad_pbrs) if broad_pbrs else None

    result_industries: dict[str, Any] = {}
    for industry in industries:
        key = str(industry.get("key"))
        basket: list[str] = []
        for code in list(industry.get("krx_basket") or []) + _theme_kr_tickers(industry, boom_snapshot):
            normalized = _ticker(code)
            if normalized and normalized not in basket:
                basket.append(normalized)
        members = [code for code in basket if code in price_all]
        changes = [finite(price_all.get(code, {}).get("change_pct")) for code in members]
        changes = [float(v) for v in changes if v is not None]
        closes = {code: roundn(price_all.get(code, {}).get("close"), 2) for code in members if finite(price_all.get(code, {}).get("close")) is not None}
        breadth = (sum(v > 0 for v in changes) / len(changes)) if changes else None
        median_change = median(changes) if changes else None
        momentum_score = None
        if median_change is not None and breadth is not None:
            momentum_score = clamp(50.0 + clamp(median_change / 20.0, -1.0, 1.0) * 30.0 + (breadth - 0.5) * 40.0, 0.0, 100.0)

        flow_signs: list[int] = []
        for code in members:
            f = finite(flow_foreign.get(code, {}).get("net_value"), 0.0) or 0.0
            inst = finite(flow_institution.get(code, {}).get("net_value"), 0.0) or 0.0
            if code in flow_foreign or code in flow_institution:
                flow_signs.append(1 if (f + inst) > 0 else (-1 if (f + inst) < 0 else 0))
        flow_positive_share = (sum(v > 0 for v in flow_signs) / len(flow_signs)) if flow_signs else None
        flow_score = clamp(50.0 + (flow_positive_share - 0.5) * 80.0, 0.0, 100.0) if flow_positive_share is not None else None

        market_pieces: list[tuple[float, float]] = []
        if momentum_score is not None:
            market_pieces.append((momentum_score, 0.7))
        if flow_score is not None:
            market_pieces.append((flow_score, 0.3))
        market_score = (sum(v*w for v,w in market_pieces)/sum(w for _,w in market_pieces)) if market_pieces else None

        sector_pers = [float(fundamental_all[code]["per"]) for code in members if code in fundamental_all and finite(fundamental_all[code].get("per")) and float(fundamental_all[code]["per"]) > 0]
        sector_pbrs = [float(fundamental_all[code]["pbr"]) for code in members if code in fundamental_all and finite(fundamental_all[code].get("pbr")) and float(fundamental_all[code]["pbr"]) > 0]
        valuation_score = _relative_valuation_score(sector_pers, sector_pbrs, broad_per, broad_pbr)

        requested = len(basket)
        member_cov = (len(members) / requested) if requested else 0.0
        breadth_penalty = min(1.0, len(members) / 4.0)
        market_quality = round(100 * (0.65 * member_cov + 0.35 * breadth_penalty), 1) if market_score is not None else 0.0
        if len(members) <= 1:
            market_quality = min(market_quality, 55.0)
        valuation_cov = (max(len(sector_pers), len(sector_pbrs)) / len(members)) if members else 0.0
        valuation_quality = round(100 * (0.65 * valuation_cov + 0.35 * breadth_penalty), 1) if valuation_score is not None else 0.0
        if len(members) <= 1:
            valuation_quality = min(valuation_quality, 50.0)

        result_industries[key] = {
            "label": industry.get("label"),
            "requested_basket": basket,
            "usable_members": members,
            "member_coverage": round(member_cov, 4),
            "period_start": start,
            "period_end": end,
            "median_return_pct": roundn(median_change, 3),
            "positive_breadth": roundn(breadth, 4),
            "flow_positive_share": roundn(flow_positive_share, 4),
            "market_internal_score": roundn(market_score, 2),
            "market_internal_quality": market_quality,
            "valuation_score": roundn(valuation_score, 2),
            "valuation_quality": valuation_quality,
            "median_per": roundn(median(sector_pers), 3) if sector_pers else None,
            "median_pbr": roundn(median(sector_pbrs), 3) if sector_pbrs else None,
            "broad_market_median_per": roundn(broad_per, 3),
            "broad_market_median_pbr": roundn(broad_pbr, 3),
            "member_closes": closes,
            "source": "KRX market-wide bulk tables via pykrx; local industry filtering",
            "source_type": "direct_sector_basket",
        }

    result = {
        "schema_version": "1.0.0",
        "generated_at_utc": utc_now_iso(),
        "available": any(v.get("market_internal_score") is not None for v in result_industries.values()),
        "stale": False,
        "source_mode": "live-pykrx-bulk",
        "normal_live_calls": calls,
        "normal_target_calls": 8,
        "fallback_calls_only_on_failure": True,
        "period_start": start,
        "period_end": end,
        "industries": result_industries,
        "diagnostics": diagnostics,
        "limitations": [
            "업종별 수급은 대표 바스켓 종목에서 외국인·기관의 순매수 방향 확산도를 사용하며 거래대금 대비 bp가 아닙니다.",
            "업종 밸류에이션은 대표 바스켓의 현재 PER/PBR을 전체 시장 횡단면과 비교한 상대값이며 장기 역사백분위가 아닙니다.",
            "대표 바스켓 수가 1개뿐인 산업은 시장내부·밸류에이션 품질점수를 자동으로 제한합니다.",
        ],
    }
    write_json(cache_path, result)
    return result
