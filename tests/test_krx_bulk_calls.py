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
