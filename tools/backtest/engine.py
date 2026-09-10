"""Bar-by-bar simulation with futures economics.

The two ways a backtest lies to you are lookahead and free execution. Both are
closed off structurally here rather than left to the strategy to get right.

**Lookahead.** A strategy returns the position it *wants* after seeing a bar
close. The engine holds that position from the *next* bar's open. A strategy
cannot express "buy at this bar's close having seen this bar's close", because
the shift happens in the engine, not the strategy.

**Execution.** Position changes fill at the open of the bar they take effect,
paying commission and slippage per contract. Profit and loss splits at that
open: the old position carries the gap from the previous close, the new position
carries the move through the bar. Attributing the whole bar to the new position
would hand it a gap it was not on the right side of.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from tools.backtest.contracts import Contract


@dataclass
class Trade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    direction: int          # +1 long, -1 short
    contracts: float
    entry_price: float
    exit_price: float
    pnl: float              # net of costs, account currency
    bars_held: int


@dataclass
class Result:
    contract: Contract
    equity: pd.Series        # cumulative net P&L in account currency
    position: pd.Series      # contracts held during each bar
    gross_pnl: pd.Series     # per-bar, before costs
    costs: pd.Series         # per-bar
    trades: list[Trade] = field(default_factory=list)

    @property
    def net_pnl(self) -> pd.Series:
        return self.gross_pnl - self.costs

    @property
    def total(self) -> float:
        return float(self.net_pnl.sum())


def run(
    df: pd.DataFrame,
    target: pd.Series,
    contract: Contract,
    *,
    commission: float | None = None,
    slippage_ticks: float | None = None,
) -> Result:
    """Simulate holding ``target`` contracts, decided at each bar's close.

    ``target`` is indexed like ``df``; the engine shifts it by one bar, so
    ``target[i]`` is the position held from ``open[i+1]`` onwards.
    """
    if not df.index.equals(target.index):
        raise ValueError("target must share the bar index")
    if len(df) < 2:
        raise ValueError("need at least two bars to simulate")

    cost_each = contract.cost_per_contract_traded()
    if commission is not None or slippage_ticks is not None:
        commission = contract.commission if commission is None else commission
        slippage_ticks = (
            contract.slippage_ticks if slippage_ticks is None else slippage_ticks
        )
        cost_each = commission + slippage_ticks * contract.tick_value

    open_ = df["open"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)

    # Position held through bar i, decided from data through bar i-1.
    pos = np.concatenate([[0.0], target.to_numpy(dtype=float)[:-1]])
    prev = np.concatenate([[0.0], pos[:-1]])

    gap = np.zeros_like(close)
    gap[1:] = prev[1:] * (open_[1:] - close[:-1])
    intrabar = pos * (close - open_)
    gross = (gap + intrabar) * contract.multiplier

    traded = np.abs(pos - prev)
    costs = traded * cost_each

    index = df.index
    return Result(
        contract=contract,
        equity=pd.Series(np.cumsum(gross - costs), index=index, name="equity"),
        position=pd.Series(pos, index=index, name="position"),
        gross_pnl=pd.Series(gross, index=index, name="gross_pnl"),
        costs=pd.Series(costs, index=index, name="costs"),
        trades=_extract_trades(index, open_, close, pos, contract.multiplier, cost_each),
    )


def _extract_trades(index, open_, close, pos, multiplier, cost_each) -> list[Trade]:
    """Round trips: flat-to-flat, with a reversal closing one and opening the next.

    P&L comes from the entry and exit prices rather than by summing the per-bar
    series. Summing is easy to get subtly wrong: a position closing at bar i's
    open still earned the gap from bar i-1's close, which is booked *in* bar i
    alongside the incoming position's move through that bar. Pricing the round
    trip directly cannot drift from the equity curve, and the invariant is
    asserted in the tests -- trade P&L sums to the run total when it ends flat.
    """
    changes = np.flatnonzero(np.diff(pos, prepend=0.0))
    trades: list[Trade] = []
    start: int | None = None

    def close_at(entry: int, exit_i: int, exit_price: float, charge_exit: bool) -> Trade:
        size = float(pos[entry])
        gross = size * (exit_price - open_[entry]) * multiplier
        charged = abs(size) * cost_each * (2 if charge_exit else 1)
        return Trade(
            entry_time=index[entry],
            exit_time=index[exit_i],
            direction=int(np.sign(size)),
            contracts=abs(size),
            entry_price=float(open_[entry]),
            exit_price=float(exit_price),
            pnl=float(gross - charged),
            bars_held=int(exit_i - entry),
        )

    for i in changes:
        if start is not None:
            trades.append(close_at(start, int(i), float(open_[i]), charge_exit=True))
            start = None
        if pos[i] != 0:
            start = int(i)

    if start is not None:
        # Still open at the end: marked to the last close, no exit cost charged
        # because no exit was paid for.
        trades.append(close_at(start, len(pos) - 1, float(close[-1]), charge_exit=False))
    return trades
