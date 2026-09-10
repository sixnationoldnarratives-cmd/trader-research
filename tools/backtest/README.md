# backtest

Strategy simulation over the `bar-data/` tree, with futures economics.

Requires numpy and pandas (`pip install -r tools/backtest/requirements.txt`).
The downloader in `tools/cfd` stays standard-library only; this package does not.

```sh
python3 -m tools.backtest contracts        # what can be tested, and against what
python3 -m tools.backtest run --contract GC --strategy ma_cross \
    --fast 20 --slow 100 --resample 1h --start 2015-01-01
```

## What the engine guarantees

**No lookahead, structurally.** A strategy returns the position it wants after
seeing a bar close; the engine holds it from the *next* bar's open. A strategy
cannot express "trade at this bar's close having seen it", because the shift
happens in the engine. `test_hindsight_signal_cannot_capture_its_own_bar` pins
this: a rule that peeks at each bar's own direction ends up losing.

**Execution is paid for.** Position changes fill at the open of the bar they
take effect and pay commission plus slippage per contract. P&L splits at that
open — the outgoing position carries the gap from the previous close, the new
one carries the move through the bar. Giving the whole bar to the new position
would hand it a gap it was not on the right side of.

**Points are not money.** Each contract carries its real multiplier and tick, so
a gold backtest reports dollars on 100 ounces, not index points.

Validated end to end against the committed data: `buy_and_hold` on GC from 2015
returns gross P&L equal to gold's move times 100 to the dollar, and `flat`
returns exactly zero.

## Market-closed padding

The feed emits flat, zero-volume bars while the market is shut — about 7% of
XAUUSD's minutes, in the daily break and over weekends. `data.load` drops them
by default. Left in, they add long runs of zero return: measured volatility on
2024 gold comes out 4.3% low, and moving averages and ATR flatten with it.

Zero-volume bars whose price *moved* are kept — those are quotes the feed
recorded without size, and dropping them would discard real price action.

## Proxies

These are CFDs, not contracts. `contracts.py` records how far each stands in for
the real thing, and the runner prints it — including a warning when a strategy
holds a weak proxy across session gaps, which is exactly where CFD and contract
diverge. A crude-oil result that looks good on a series with no roll yield is
the classic way to lose money on a strategy that tested well.

## Writing a strategy

Take the bar frame, return the position in contracts wanted after each bar:

```python
def my_rule(df, lookback=50, contracts=1.0):
    breakout = df["close"] > df["high"].rolling(lookback).max().shift(1)
    return breakout.astype(float) * contracts
```

Register it in `strategies.REGISTRY`. The `.shift(1)` there is about the
*channel* excluding the current bar; the engine handles execution timing.
