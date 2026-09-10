import unittest

import pandas as pd

from tools.backtest import metrics, strategies
from tools.backtest.contracts import Contract, get
from tools.backtest.engine import run

# multiplier 10, tick 1 -> tick_value 10; costs switched off unless a test wants them
TOY = Contract("TOY", "toy", "TOYCFD", multiplier=10.0, tick=1.0, currency="USD",
               proxy_quality="good", commission=0.0, slippage_ticks=0.0)


def frame(bars):
    """bars: list of (open, close). High/low bracket them, volume nonzero."""
    index = pd.date_range("2026-01-01", periods=len(bars), freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [b[0] for b in bars],
            "close": [b[1] for b in bars],
            "high": [max(b) for b in bars],
            "low": [min(b) for b in bars],
            "volume": [1] * len(bars),
        },
        index=index,
    )


def const(df, value):
    return pd.Series(float(value), index=df.index)


class ExecutionTest(unittest.TestCase):
    def setUp(self):
        # rises 100->101, 102->103, 104->105 with gaps between bars
        self.df = frame([(100, 101), (102, 103), (104, 105)])

    def test_flat_earns_exactly_zero(self):
        result = run(self.df, strategies.flat(self.df), TOY)
        self.assertEqual(result.total, 0.0)
        self.assertEqual(len(result.trades), 0)

    def test_position_is_held_from_the_next_bar_open(self):
        result = run(self.df, const(self.df, 1), TOY)
        # decided at close of each bar, so bar 0 is still flat
        self.assertEqual(list(result.position), [0.0, 1.0, 1.0])

    def test_pnl_matches_hand_calculation(self):
        # entered at open of bar 1 (102), marked to close of bar 2 (105)
        # 3 points x multiplier 10 = 30
        result = run(self.df, const(self.df, 1), TOY)
        self.assertAlmostEqual(result.total, 30.0)

    def test_short_is_the_mirror_of_long(self):
        long_ = run(self.df, const(self.df, 1), TOY).total
        short = run(self.df, const(self.df, -1), TOY).total
        self.assertAlmostEqual(short, -long_)

    def test_size_scales_pnl_linearly(self):
        one = run(self.df, const(self.df, 1), TOY).total
        three = run(self.df, const(self.df, 3), TOY).total
        self.assertAlmostEqual(three, 3 * one)


class NoLookaheadTest(unittest.TestCase):
    def test_hindsight_signal_cannot_capture_its_own_bar(self):
        # bar 1 rises 4 points, bar 2 falls 4. A strategy that "sees" each bar's
        # own close and goes long on up-bars would make money if the engine let
        # it. Shifted properly it is long for bar 2 instead, and loses.
        df = frame([(100, 100), (100, 104), (104, 100)])
        hindsight = (df["close"] > df["open"]).astype(float)
        result = run(df, hindsight, TOY)
        self.assertEqual(list(result.position), [0.0, 0.0, 1.0])
        self.assertLess(result.total, 0)
        self.assertAlmostEqual(result.total, -40.0)  # 4 points down x 10

    def test_a_signal_on_the_final_bar_never_trades(self):
        df = frame([(100, 100), (100, 100), (100, 100)])
        target = pd.Series([0.0, 0.0, 5.0], index=df.index)
        result = run(df, target, TOY)
        self.assertEqual(result.position.iloc[-1], 0.0)


class GapAttributionTest(unittest.TestCase):
    def test_gap_belongs_to_the_position_that_was_held_across_it(self):
        # long through bar 1, flat after. Bar 2 opens 10 points higher: that gap
        # was earned by the position held overnight, not by the flat one.
        df = frame([(100, 100), (100, 110), (120, 120)])
        target = pd.Series([1.0, 0.0, 0.0], index=df.index)
        result = run(df, target, TOY)
        self.assertEqual(list(result.position), [0.0, 1.0, 0.0])
        # bar 1: 100->110 = 10 pts; bar 2 gap 110->120 = 10 pts, still long into it
        self.assertAlmostEqual(result.total, 200.0)


class CostTest(unittest.TestCase):
    def setUp(self):
        self.df = frame([(100, 100), (100, 100), (100, 100)])
        self.costly = Contract("C", "c", "P", multiplier=10.0, tick=1.0, currency="USD",
                               proxy_quality="good", commission=2.0, slippage_ticks=1.0)

    def test_cost_per_contract_is_commission_plus_slippage(self):
        # tick_value = 10 * 1; one tick of slippage + 2.00 commission
        self.assertAlmostEqual(self.costly.cost_per_contract_traded(), 12.0)

    def test_entry_is_charged_once_per_contract(self):
        result = run(self.df, const(self.df, 1), self.costly)
        self.assertAlmostEqual(result.costs.sum(), 12.0)
        self.assertAlmostEqual(result.total, -12.0)  # flat market, cost only

    def test_reversal_is_charged_on_both_contracts(self):
        target = pd.Series([1.0, -1.0, -1.0], index=self.df.index)
        result = run(self.df, target, self.costly)
        # 1 contract on, then 2 traded to flip long->short
        self.assertAlmostEqual(result.costs.sum(), 12.0 + 24.0)

    def test_overrides_beat_the_contract_defaults(self):
        result = run(self.df, const(self.df, 1), self.costly,
                     commission=0.0, slippage_ticks=0.0)
        self.assertAlmostEqual(result.costs.sum(), 0.0)


class TradeExtractionTest(unittest.TestCase):
    def test_round_trip_is_recorded_with_entry_and_exit(self):
        df = frame([(100, 100), (100, 110), (110, 110), (120, 120)])
        target = pd.Series([1.0, 1.0, 0.0, 0.0], index=df.index)
        result = run(df, target, TOY)
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.direction, 1)
        self.assertAlmostEqual(trade.entry_price, 100.0)
        self.assertAlmostEqual(trade.exit_price, 120.0)
        self.assertAlmostEqual(trade.pnl, result.total)

    def test_trade_pnl_sums_to_the_run_total_when_it_ends_flat(self):
        # The invariant that catches a trade-accounting drift: if every position
        # was closed, the trades must account for the whole equity curve.
        df = frame([(100, 105), (108, 103), (101, 109), (112, 110), (110, 110)])
        target = pd.Series([1.0, 1.0, -2.0, 0.0, 0.0], index=df.index)
        costly = Contract("C", "c", "P", multiplier=10.0, tick=1.0, currency="USD",
                          proxy_quality="good", commission=2.0, slippage_ticks=1.0)
        result = run(df, target, costly)
        self.assertEqual(result.position.iloc[-1], 0.0)
        self.assertAlmostEqual(sum(t.pnl for t in result.trades), result.total)

    def test_open_trade_pnl_also_sums_to_the_run_total(self):
        df = frame([(100, 105), (108, 103), (101, 109)])
        result = run(df, const(df, 2), TOY)
        self.assertAlmostEqual(sum(t.pnl for t in result.trades), result.total)

    def test_reversal_closes_one_trade_and_opens_another(self):
        df = frame([(100, 100), (100, 100), (100, 100), (100, 100)])
        target = pd.Series([1.0, -1.0, -1.0, 0.0], index=df.index)
        result = run(df, target, TOY)
        self.assertEqual([t.direction for t in result.trades], [1, -1])

    def test_open_position_at_the_end_is_still_reported(self):
        df = frame([(100, 100), (100, 110), (110, 120)])
        result = run(df, const(df, 1), TOY)
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].exit_time, df.index[-1])


class ContractTest(unittest.TestCase):
    def test_real_contract_tick_values(self):
        self.assertAlmostEqual(get("GC").tick_value, 10.00)    # 100oz x $0.10
        self.assertAlmostEqual(get("NQ").tick_value, 5.00)     # $20 x 0.25
        self.assertAlmostEqual(get("CL").tick_value, 10.00)    # 1000bbl x $0.01
        self.assertAlmostEqual(get("HG").tick_value, 12.50)    # 25000lb x $0.0005
        self.assertAlmostEqual(get("FDAX").tick_value, 25.00)  # EUR25 x 1.0

    def test_unknown_contract_lists_what_is_available(self):
        with self.assertRaises(KeyError) as ctx:
            get("ZZ")
        self.assertIn("GC", str(ctx.exception))


class MetricsTest(unittest.TestCase):
    def test_flat_run_reports_nothing_happened(self):
        df = frame([(100, 100)] * 10)
        stats = metrics.compute(run(df, strategies.flat(df), TOY))
        self.assertEqual(stats.net_pnl, 0.0)
        self.assertEqual(stats.trades, 0)
        self.assertEqual(stats.exposure, 0.0)
        self.assertEqual(stats.sharpe, 0.0)

    def test_drawdown_is_measured_from_the_running_peak(self):
        # up 10 pts, then down 30, then up 10  (x multiplier 10)
        df = frame([(100, 100), (100, 110), (110, 80), (80, 90)])
        stats = metrics.compute(run(df, const(df, 1), TOY))
        self.assertAlmostEqual(stats.max_drawdown, 300.0)

    def test_exposure_counts_bars_holding_a_position(self):
        df = frame([(100, 100)] * 4)
        target = pd.Series([1.0, 1.0, 0.0, 0.0], index=df.index)
        stats = metrics.compute(run(df, target, TOY))
        self.assertAlmostEqual(stats.exposure, 0.5)


class StrategyTest(unittest.TestCase):
    def test_ma_cross_rejects_an_inverted_pair(self):
        df = frame([(100, 100)] * 5)
        with self.assertRaises(ValueError):
            strategies.ma_cross(df, fast=50, slow=10)

    def test_ma_cross_is_flat_until_the_slow_window_fills(self):
        df = frame([(100, 100 + i) for i in range(30)])
        signal = strategies.ma_cross(df, fast=3, slow=10)
        self.assertTrue((signal.iloc[:9] == 0).all())

    def test_donchian_channel_excludes_the_current_bar(self):
        # a new high on the final bar must be compared against earlier bars only
        df = frame([(100, 100)] * 5 + [(100, 150)])
        signal = strategies.donchian(df, lookback=3)
        self.assertEqual(signal.iloc[-1], 1.0)


if __name__ == "__main__":
    unittest.main()
