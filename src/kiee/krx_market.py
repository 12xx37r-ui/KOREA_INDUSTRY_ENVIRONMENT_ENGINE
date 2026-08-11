from __future__ import annotations

import math
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

from .util import age_hours, clamp, finite, read_json, roundn, utc_now_iso, write_json

KST = timezone(timedelta(hours=9))


def _frame_empty(frame: Any) -> bool:
    return frame is None or bool(getattr(frame, "empty", True))


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


def _latest_completed_market_date() -> date:
    """Return a conservative KRX date for end-of-day investor/fundamental data.

    KRX investor totals are finalized after the close. Before 19:00 KST we use the
    prior weekday. Holiday handling is performed by bounded backtracking when the
    live table is actually fetched, so no extra calendar API is needed.
    """
    now = datetime.now(KST)
    candidate = now.date()
    if now.hour < 19:
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _business_window(days: int = 35) -> tuple[str, str]:
    end_date = _latest_completed_market_date()
    start_date = end_date - timedelta(days=max(28, int(days)))
    return start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d")


def _backtrack_business_dates(anchor: str, limit: int = 5) -> list[str]:
    try:
        d = datetime.strptime(str(anchor), "%Y%m%d").date()
    except Exception:
        d = _latest_completed_market_date()
    out: list[str] = []
    while len(out) < max(1, int(limit)):
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return out


def _frame_has_any_column(frame: Any, candidates: tuple[str, ...]) -> bool:
    if _frame_empty(frame):
        return False
    columns = {str(x) for x in getattr(frame, "columns", [])}
    return any(name in columns for name in candidates)


def _flow_frame(stock: Any, start: str, end: str, market: str, investor: str, diagnostics: list[str]) -> tuple[Any, int]:
    attempts = 0
    fn = getattr(stock, "get_market_net_purchases_of_equities_by_ticker", None)
    if callable(fn):
        attempts += 1
        try:
            frame = fn(start, end, market=market, investor=investor)
            if not _frame_empty(frame):
                return frame, attempts
            diagnostics.append(f"flow:{market}:{investor}:primary:empty")
        except Exception as exc:
            diagnostics.append(f"flow:{market}:{investor}:primary:{type(exc).__name__}:{str(exc)[:120]}")
    # Fallback is attempted only when the normal API is unavailable/failed/empty.
    fn2 = getattr(stock, "get_market_trading_value_by_ticker", None)
    if callable(fn2):
        attempts += 1
        try:
            frame = fn2(start, end, market=market, investor=investor)
            if not _frame_empty(frame):
                return frame, attempts
            diagnostics.append(f"flow:{market}:{investor}:fallback:empty")
        except Exception as exc:
            diagnostics.append(f"flow:{market}:{investor}:fallback:{type(exc).__name__}:{str(exc)[:120]}")
    return None, attempts


def _relative_valuation_score(
    sector_pers: list[float],
    sector_pbrs: list[float],
    broad_per: float | None,
    broad_pbr: float | None,
) -> float | None:
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


def _fetch_price_change(
    stock: Any,
    start: str,
    end: str,
    market: str,
    diagnostics: list[str],
) -> tuple[dict[str, dict[str, float | None]], int, str, str]:
    """Fetch period return table in one normal call; bounded OHLCV snapshots are fallback.

    Normal path preserves the original 2 market-wide price calls. If KRX returns an
    empty/error frame, two single-day market-wide snapshots are used to compute the
    period return locally. This is still market-wide batching, never per company.
    """
    attempts = 0
    fn = getattr(stock, "get_market_price_change_by_ticker", None)
    if callable(fn):
        attempts += 1
        try:
            frame = fn(start, end, market=market)
            rows = _rows(frame)
            if rows and any(finite(row.get("change_pct")) is not None for row in rows.values()):
                return rows, attempts, start, end
            diagnostics.append(f"price:{market}:primary_empty_or_columns:{list(getattr(frame, 'columns', []))}")
        except Exception as exc:
            diagnostics.append(f"price:{market}:primary:{type(exc).__name__}:{str(exc)[:140]}")

    snapshot_fn = getattr(stock, "get_market_ohlcv_by_ticker", None)
    if not callable(snapshot_fn):
        diagnostics.append(f"price:{market}:ohlcv_fallback_unavailable")
        return {}, attempts, start, end

    def fetch_snapshot(anchor: str, role: str) -> tuple[dict[str, dict[str, float | None]], str | None, int]:
        local_attempts = 0
        for trading_date in _backtrack_business_dates(anchor, 4):
            local_attempts += 1
            try:
                frame = snapshot_fn(trading_date, market=market)
                rows = _rows(frame)
                if rows and any(finite(row.get("close")) is not None for row in rows.values()):
                    if trading_date != anchor:
                        diagnostics.append(f"price:{market}:{role}:backtracked:{anchor}->{trading_date}")
                    return rows, trading_date, local_attempts
                diagnostics.append(f"price:{market}:{role}:{trading_date}:empty_or_close_missing")
            except Exception as exc:
                diagnostics.append(f"price:{market}:{role}:{trading_date}:{type(exc).__name__}:{str(exc)[:120]}")
        return {}, None, local_attempts

    start_rows, actual_start, c1 = fetch_snapshot(start, "start")
    end_rows, actual_end, c2 = fetch_snapshot(end, "end")
    attempts += c1 + c2
    if not start_rows or not end_rows:
        return {}, attempts, actual_start or start, actual_end or end

    out: dict[str, dict[str, float | None]] = {}
    for code, end_row in end_rows.items():
        end_close = finite(end_row.get("close"))
        start_close = finite(start_rows.get(code, {}).get("close"))
        if end_close is None:
            continue
        change_pct = None
        if start_close is not None and start_close > 0:
            change_pct = (end_close / start_close - 1.0) * 100.0
        out[code] = {"close": end_close, "change_pct": change_pct}
    if out:
        diagnostics.append(f"price:{market}:fallback_two_snapshot_local_return")
    return out, attempts, actual_start or start, actual_end or end


def _fetch_fundamental(
    stock: Any,
    end: str,
    market: str,
    diagnostics: list[str],
) -> tuple[dict[str, dict[str, float | None]], int, str]:
    """Fetch market-wide PER/PBR with bounded prior-business-day fallback."""
    fn = getattr(stock, "get_market_fundamental_by_ticker", None)
    if not callable(fn):
        # pykrx public API also exposes get_market_fundamental; keep compatibility
        # with versions/wrappers that do not retain the *_by_ticker alias.
        fn = getattr(stock, "get_market_fundamental", None)
    if not callable(fn):
        diagnostics.append(f"fundamental:{market}:api_unavailable")
        return {}, 0, end

    attempts = 0
    for trading_date in _backtrack_business_dates(end, 4):
        attempts += 1
        try:
            frame = fn(trading_date, market=market)
            rows = _rows(frame)
            valid = rows and any(
                finite(row.get("per")) is not None or finite(row.get("pbr")) is not None
                for row in rows.values()
            )
            if valid:
                if trading_date != end:
                    diagnostics.append(f"fundamental:{market}:backtracked:{end}->{trading_date}")
                return rows, attempts, trading_date
            diagnostics.append(
                f"fundamental:{market}:{trading_date}:empty_or_columns:{list(getattr(frame, 'columns', []))}"
            )
        except Exception as exc:
            diagnostics.append(f"fundamental:{market}:{trading_date}:{type(exc).__name__}:{str(exc)[:140]}")
    return {}, attempts, end


def _previous_reusable(previous: dict[str, Any], max_lkg_age_hours: float) -> tuple[bool, float | None]:
    previous_age = age_hours(previous.get("generated_at_utc")) if isinstance(previous, dict) else None
    reusable = bool(
        isinstance(previous, dict)
        and previous.get("industries")
        and previous_age is not None
        and previous_age <= max_lkg_age_hours
    )
    return reusable, previous_age


def _reuse_full_lkg(
    previous: dict[str, Any],
    previous_age: float | None,
    max_lkg_age_hours: float,
    mode: str,
    diagnostics: list[str],
    credentials_configured: bool,
) -> dict[str, Any]:
    reused = dict(previous)
    reused["stale"] = True
    reused["source_mode"] = mode
    reused["lkg_age_hours"] = roundn(previous_age, 2)
    reused["lkg_max_age_hours"] = max_lkg_age_hours
    reused["krx_credentials_configured"] = credentials_configured
    reused["normal_live_calls"] = 0
    reused["diagnostics"] = list(reused.get("diagnostics") or []) + diagnostics
    return reused


def collect_sector_market(
    root: Path,
    industries: list[dict[str, Any]],
    boom_snapshot: dict[str, Any],
    stock_module: Any = None,
    allow_live: bool = True,
    max_lkg_age_hours: float = 120.0,
) -> dict[str, Any]:
    """Bulk KRX market-internal snapshot.

    Normal live budget remains 8 calls:
      * 2 market-wide period price-change tables
      * 4 market-wide investor-flow tables
      * 2 market-wide fundamental tables

    Fallback calls happen only when a normal KRX table is empty/errored. All 25
    industries are still filtered locally from the bulk tables; no per-company call
    is ever made.
    """
    cache_path = root / "input_cache" / "latest" / "krx_sector_market.json"
    previous = read_json(cache_path, {}) or {}
    diagnostics: list[str] = []
    calls = 0
    start, end = _business_window(35)

    credentials_configured = bool(os.getenv("KRX_ID") and os.getenv("KRX_PW"))
    injected_stock = stock_module is not None
    previous_reusable, previous_age = _previous_reusable(previous, max_lkg_age_hours)

    # pykrx >= 1.2.x supports KRX_ID/KRX_PW based session management. In CI the
    # repository secrets must be explicitly mapped into the workflow environment.
    # If they are absent, do not hammer KRX with calls known to return empty/LOGOUT.
    stock = stock_module
    if stock is None and allow_live and not credentials_configured:
        diagnostics.append("krx_credentials_missing: set repository secrets KRX_ID and KRX_PW")
        if previous_reusable:
            return _reuse_full_lkg(
                previous, previous_age, max_lkg_age_hours, "lkg-no-credentials",
                diagnostics, credentials_configured,
            )
        return {
            "schema_version": "1.0.2",
            "generated_at_utc": utc_now_iso(),
            "available": False,
            "stale": False,
            "source_mode": "credentials-missing",
            "normal_live_calls": 0,
            "normal_target_calls": 8,
            "max_fallback_calls": 20,
            "krx_credentials_configured": False,
            "industries": {},
            "diagnostics": diagnostics,
        }

    if stock is None and allow_live:
        try:
            from pykrx import stock as pykrx_stock  # type: ignore
            stock = pykrx_stock
        except Exception as exc:
            diagnostics.append(f"pykrx_import:{type(exc).__name__}:{exc}")

    if stock is None:
        if previous_reusable:
            return _reuse_full_lkg(
                previous, previous_age, max_lkg_age_hours, "lkg-no-pykrx",
                diagnostics, credentials_configured,
            )
        if isinstance(previous, dict) and previous.get("industries"):
            diagnostics.append(
                "KRX LKG expired or timestamp unavailable: age=" + str(roundn(previous_age, 2))
                + "h max=" + str(max_lkg_age_hours) + "h"
            )
        return {
            "schema_version": "1.0.2", "generated_at_utc": utc_now_iso(), "available": False,
            "stale": False, "source_mode": "unavailable", "normal_live_calls": 0,
            "normal_target_calls": 8, "max_fallback_calls": 20,
            "krx_credentials_configured": credentials_configured,
            "industries": {}, "diagnostics": diagnostics or ["pykrx unavailable"],
        }

    price_all: dict[str, dict[str, float | None]] = {}
    fundamental_all: dict[str, dict[str, float | None]] = {}
    flow_foreign: dict[str, dict[str, float | None]] = {}
    flow_institution: dict[str, dict[str, float | None]] = {}
    actual_periods: dict[str, dict[str, str]] = {}

    for market in ("KOSPI", "KOSDAQ"):
        price_rows, price_calls, actual_start, actual_end = _fetch_price_change(
            stock, start, end, market, diagnostics
        )
        calls += price_calls
        _merge(price_all, price_rows)
        actual_periods[market] = {"start": actual_start, "end": actual_end}

        fund_rows, fund_calls, fund_date = _fetch_fundamental(stock, actual_end or end, market, diagnostics)
        calls += fund_calls
        _merge(fundamental_all, fund_rows)
        actual_periods[market]["fundamental_date"] = fund_date

        for investor, target in (("외국인", flow_foreign), ("기관합계", flow_institution)):
            frame, flow_calls = _flow_frame(stock, actual_start or start, actual_end or end, market, investor, diagnostics)
            calls += flow_calls
            _merge(target, _rows(frame))

    broad_pers = [
        float(v.get("per")) for v in fundamental_all.values()
        if finite(v.get("per")) is not None and float(v.get("per")) > 0
    ]
    broad_pbrs = [
        float(v.get("pbr")) for v in fundamental_all.values()
        if finite(v.get("pbr")) is not None and float(v.get("pbr")) > 0
    ]
    broad_per = median(broad_pers) if broad_pers else None
    broad_pbr = median(broad_pbrs) if broad_pbrs else None

    result_industries: dict[str, Any] = {}
    fresh_industries = 0
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
        closes = {
            code: roundn(price_all.get(code, {}).get("close"), 2)
            for code in members if finite(price_all.get(code, {}).get("close")) is not None
        }
        breadth = (sum(v > 0 for v in changes) / len(changes)) if changes else None
        median_change = median(changes) if changes else None
        momentum_score = None
        if median_change is not None and breadth is not None:
            momentum_score = clamp(
                50.0 + clamp(median_change / 20.0, -1.0, 1.0) * 30.0 + (breadth - 0.5) * 40.0,
                0.0,
                100.0,
            )

        flow_signs: list[int] = []
        for code in members:
            f = finite(flow_foreign.get(code, {}).get("net_value"), 0.0) or 0.0
            inst = finite(flow_institution.get(code, {}).get("net_value"), 0.0) or 0.0
            if code in flow_foreign or code in flow_institution:
                flow_signs.append(1 if (f + inst) > 0 else (-1 if (f + inst) < 0 else 0))
        flow_positive_share = (sum(v > 0 for v in flow_signs) / len(flow_signs)) if flow_signs else None
        flow_score = (
            clamp(50.0 + (flow_positive_share - 0.5) * 80.0, 0.0, 100.0)
            if flow_positive_share is not None else None
        )

        market_pieces: list[tuple[float, float]] = []
        if momentum_score is not None:
            market_pieces.append((momentum_score, 0.7))
        if flow_score is not None:
            market_pieces.append((flow_score, 0.3))
        market_score = (
            sum(v * w for v, w in market_pieces) / sum(w for _, w in market_pieces)
            if market_pieces else None
        )

        sector_pers = [
            float(fundamental_all[code]["per"]) for code in members
            if code in fundamental_all
            and finite(fundamental_all[code].get("per")) is not None
            and float(fundamental_all[code]["per"]) > 0
        ]
        sector_pbrs = [
            float(fundamental_all[code]["pbr"]) for code in members
            if code in fundamental_all
            and finite(fundamental_all[code].get("pbr")) is not None
            and float(fundamental_all[code]["pbr"]) > 0
        ]
        valuation_score = _relative_valuation_score(sector_pers, sector_pbrs, broad_per, broad_pbr)

        requested = len(basket)
        member_cov = (len(members) / requested) if requested else 0.0
        breadth_penalty = min(1.0, len(members) / 4.0)
        market_quality = (
            round(100 * (0.65 * member_cov + 0.35 * breadth_penalty), 1)
            if market_score is not None else 0.0
        )
        if len(members) <= 1:
            market_quality = min(market_quality, 55.0)
        valuation_cov = (max(len(sector_pers), len(sector_pbrs)) / len(members)) if members else 0.0
        valuation_quality = (
            round(100 * (0.65 * valuation_cov + 0.35 * breadth_penalty), 1)
            if valuation_score is not None else 0.0
        )
        if len(members) <= 1:
            valuation_quality = min(valuation_quality, 50.0)

        row = {
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
            "stale": False,
        }
        if market_score is not None or valuation_score is not None:
            fresh_industries += 1
        result_industries[key] = row

    # Per-industry LKG merge: a temporary partial KRX outage should not erase an
    # otherwise recent direct-industry signal. Stale rows are quality-discounted.
    lkg_reused = 0
    if previous_reusable:
        previous_industries = previous.get("industries") or {}
        for key, row in list(result_industries.items()):
            if row.get("market_internal_score") is not None or row.get("valuation_score") is not None:
                continue
            prior = previous_industries.get(key)
            if not isinstance(prior, dict):
                continue
            if prior.get("market_internal_score") is None and prior.get("valuation_score") is None:
                continue
            reused = dict(prior)
            reused["stale"] = True
            reused["source_type"] = "direct_sector_basket_lkg"
            reused["source"] = "KRX direct-sector LKG reused after live partial failure"
            reused["market_internal_quality"] = roundn((finite(reused.get("market_internal_quality"), 0.0) or 0.0) * 0.75, 1)
            reused["valuation_quality"] = roundn((finite(reused.get("valuation_quality"), 0.0) or 0.0) * 0.75, 1)
            reused["lkg_age_hours"] = roundn(previous_age, 2)
            result_industries[key] = reused
            lkg_reused += 1

    available_count = sum(
        1 for v in result_industries.values()
        if v.get("market_internal_score") is not None or v.get("valuation_score") is not None
    )
    result = {
        "schema_version": "1.0.2",
        "generated_at_utc": utc_now_iso(),
        "available": available_count > 0,
        "stale": bool(lkg_reused and fresh_industries == 0),
        "source_mode": "live-pykrx-bulk" if fresh_industries else ("lkg-live-failed" if lkg_reused else "live-pykrx-failed"),
        "normal_live_calls": calls,
        "normal_target_calls": 8,
        "max_fallback_calls": 20,
        "fallback_calls_only_on_failure": True,
        "krx_credentials_configured": credentials_configured or injected_stock,
        "auth_mode": "injected-test" if injected_stock else "pykrx-env-session",
        "period_start": start,
        "period_end": end,
        "actual_periods": actual_periods,
        "fresh_industry_count": fresh_industries,
        "lkg_reused_industry_count": lkg_reused,
        "available_industry_count": available_count,
        "industry_coverage_pct": roundn(available_count / len(industries) * 100.0, 1) if industries else 0.0,
        "industries": result_industries,
        "diagnostics": diagnostics,
        "limitations": [
            "업종별 수급은 대표 바스켓 종목에서 외국인·기관의 순매수 방향 확산도를 사용하며 거래대금 대비 bp가 아닙니다.",
            "업종 밸류에이션은 대표 바스켓의 현재 PER/PBR을 전체 시장 횡단면과 비교한 상대값이며 장기 역사백분위가 아닙니다.",
            "대표 바스켓 수가 1개뿐인 산업은 시장내부·밸류에이션 품질점수를 자동으로 제한합니다.",
            "KRX live 호출은 KRX_ID/KRX_PW 세션이 필요하며, 일시 실패 시 최대 120시간 LKG만 품질 할인 후 재사용합니다.",
        ],
    }

    # If every live table failed but a valid full LKG exists, prefer the prior
    # complete snapshot over a newly empty shell. This also keeps external calls
    # from cascading into repeated retries in downstream code.
    if not result["available"] and previous_reusable:
        diagnostics.append("krx_live_all_failed: full LKG reused")
        return _reuse_full_lkg(
            previous, previous_age, max_lkg_age_hours, "lkg-live-failed",
            diagnostics, credentials_configured,
        )

    write_json(cache_path, result)
    return result
