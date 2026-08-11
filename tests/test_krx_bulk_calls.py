from pathlib import Path

import pandas as pd

from kiee.config import load_all
from kiee.krx_market import collect_sector_market
from kiee.util import read_json

ROOT = Path(__file__).resolve().parents[1]


class FakeStock:
    def __init__(self):
        self.calls = []
        self.price = pd.DataFrame(
            {
                "종가": [80000, 200000, 50000, 120000, 30000, 3500, 100000],
                "등락률": [4.0, 7.0, -2.0, 3.0, 1.0, 5.0, -1.0],
            },
            index=["005930", "000660", "009540", "329180", "035900", "037270", "105560"],
        )
        self.fund = pd.DataFrame(
            {
                "PER": [18, 12, 20, 17, 28, 25, 8],
                "PBR": [1.6, 1.8, 1.3, 1.5, 4.2, 2.6, 0.7],
            },
            index=self.price.index,
        )
        self.flow_foreign = pd.DataFrame(
            {"순매수거래대금": [10, 20, -10, 30, 5, 6, -3]}, index=self.price.index
        )
        self.flow_inst = pd.DataFrame(
            {"순매수거래대금": [5, -5, 20, 10, 2, -2, 4]}, index=self.price.index
        )

    def get_market_price_change_by_ticker(self, start, end, market=None):
        self.calls.append(("price", market))
        # Real API is market split; returning same compact frame is sufficient to test call budget/local filter.
        return self.price

    def get_market_fundamental_by_ticker(self, end, market=None):
        self.calls.append(("fund", market))
        return self.fund

    def get_market_net_purchases_of_equities_by_ticker(self, start, end, market=None, investor=None):
        self.calls.append(("flow", market, investor))
        return self.flow_foreign if investor == "외국인" else self.flow_inst


def test_krx_market_is_eight_bulk_calls_not_per_company(tmp_path):
    industries_cfg, _, _ = load_all(ROOT)
    boom = read_json(ROOT / "fixtures" / "upstream" / "industry_boom_snapshot.json", {})
    fake = FakeStock()
    result = collect_sector_market(tmp_path, industries_cfg["industries"], boom, stock_module=fake, allow_live=True)
    assert result["normal_live_calls"] == 8
    assert len(fake.calls) == 8
    assert result["available"] is True
    assert result["industries"]["semiconductor"]["market_internal_score"] is not None
    assert result["industries"]["shipbuilding"]["market_internal_score"] is not None
    assert result["industries"]["media_entertainment"]["market_internal_score"] is not None

class FakeStockFlowPrimaryFails(FakeStock):
    def get_market_net_purchases_of_equities_by_ticker(self, start, end, market=None, investor=None):
        self.calls.append(("flow-primary-fail", market, investor))
        raise RuntimeError("primary unavailable")

    def get_market_trading_value_by_ticker(self, start, end, market=None, investor=None):
        self.calls.append(("flow-fallback", market, investor))
        return self.flow_foreign if investor == "외국인" else self.flow_inst


def test_krx_flow_fallback_calls_are_counted_exactly(tmp_path):
    industries_cfg, _, _ = load_all(ROOT)
    boom = read_json(ROOT / "fixtures" / "upstream" / "industry_boom_snapshot.json", {})
    fake = FakeStockFlowPrimaryFails()
    result = collect_sector_market(tmp_path, industries_cfg["industries"], boom, stock_module=fake, allow_live=True)
    # 2 price + 2 fundamentals + (4 primary failures + 4 fallback successes) = 12 attempts.
    assert result["normal_live_calls"] == 12
    assert len(fake.calls) == 12
    assert result["available"] is True

class FakeStockPrimaryPriceEmpty(FakeStock):
    def __init__(self):
        super().__init__()
        self.start_snapshot = self.price.copy()
        self.start_snapshot["종가"] = [76000, 190000, 52000, 118000, 29500, 3400, 101000]
        self.end_snapshot = self.price.copy()

    def get_market_price_change_by_ticker(self, start, end, market=None):
        self.calls.append(("price-empty", market))
        return pd.DataFrame()

    def get_market_ohlcv_by_ticker(self, day, market=None):
        self.calls.append(("ohlcv", market, day))
        # Collector asks start first, end second; exact calendar date is intentionally irrelevant here.
        same_market_ohlcv_calls = [x for x in self.calls if x[0] == "ohlcv" and x[1] == market]
        return self.start_snapshot if len(same_market_ohlcv_calls) == 1 else self.end_snapshot


def test_empty_price_change_uses_two_bulk_snapshots_and_local_return(tmp_path):
    industries_cfg, _, _ = load_all(ROOT)
    boom = read_json(ROOT / "fixtures" / "upstream" / "industry_boom_snapshot.json", {})
    fake = FakeStockPrimaryPriceEmpty()
    result = collect_sector_market(tmp_path, industries_cfg["industries"], boom, stock_module=fake, allow_live=True)
    assert result["available"] is True
    assert result["normal_live_calls"] == 12  # 2 empty primary + 4 OHLCV snapshots + 2 fundamentals + 4 flows
    semi = result["industries"]["semiconductor"]
    assert semi["usable_members"]
    assert semi["median_return_pct"] is not None
    assert any("fallback_two_snapshot_local_return" in x for x in result["diagnostics"])


class FakeStockFundamentalBacktrack(FakeStock):
    def __init__(self):
        super().__init__()
        self.fund_attempts = {"KOSPI": 0, "KOSDAQ": 0}

    def get_market_fundamental_by_ticker(self, end, market=None):
        self.calls.append(("fund", market, end))
        self.fund_attempts[market] += 1
        if self.fund_attempts[market] == 1:
            return pd.DataFrame()
        return self.fund


def test_empty_fundamental_backtracks_one_business_day(tmp_path):
    industries_cfg, _, _ = load_all(ROOT)
    boom = read_json(ROOT / "fixtures" / "upstream" / "industry_boom_snapshot.json", {})
    fake = FakeStockFundamentalBacktrack()
    result = collect_sector_market(tmp_path, industries_cfg["industries"], boom, stock_module=fake, allow_live=True)
    assert result["available"] is True
    assert result["normal_live_calls"] == 10  # normal 8 + one extra fundamental attempt per market
    assert result["industries"]["semiconductor"]["valuation_score"] is not None
    assert any("fundamental:KOSPI:backtracked" in x for x in result["diagnostics"])
