import pandas as pd

from kiee.krx_market import _fetch_fundamental, _rows


class ZeroThenValidFundamental:
    def __init__(self):
        self.calls = 0

    def get_market_fundamental_by_ticker(self, day, market=None):
        self.calls += 1
        if self.calls == 1:
            return pd.DataFrame(
                {"PER": [0.0, 0.0], "PBR": [0.0, 0.0], "DIV": [0.0, 0.0]},
                index=["005930", "000660"],
            )
        return pd.DataFrame(
            {"PER": [18.0, 12.0], "PBR": [1.6, 1.8], "DIV": [1.0, 0.8]},
            index=["005930", "000660"],
        )


def test_all_zero_fundamental_table_is_not_treated_as_live_success():
    fake = ZeroThenValidFundamental()
    diagnostics = []
    rows, attempts, actual_day = _fetch_fundamental(
        fake, "20260817", "KOSPI", diagnostics
    )

    assert attempts == 2
    assert fake.calls == 2
    assert actual_day != "20260817"
    assert rows["005930"]["per"] == 18.0
    assert rows["000660"]["pbr"] == 1.8
    assert any("zero_or_unusable_multiples" in msg for msg in diagnostics)
    assert any("backtracked" in msg for msg in diagnostics)


def test_rows_prefers_explicit_ticker_column_over_range_index():
    frame = pd.DataFrame(
        {
            "티커": ["005930", "000660"],
            "PER": [18.0, 12.0],
            "PBR": [1.6, 1.8],
        }
    )
    rows = _rows(frame)

    assert set(rows) == {"005930", "000660"}
    assert "000001" not in rows
    assert rows["005930"]["per"] == 18.0
