"""Performance statistics for a :class:`~tools.backtest.engine.Result`."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from tools.backtest.engine import Result

SECONDS_PER_YEAR = 365.25 * 24 * 3600


@dataclass
class Stats:
    bars: int
    span_years: float
    net_pnl: float
    gross_pnl: float
    costs: float
    cost_share: float          # costs as a fraction of gross, when gross > 0
    sharpe: float
    sortino: float
    max_drawdown: float        # account currency, positive number
    max_drawdown_pct: float    # against peak equity, when equity went positive
    trades: int
    win_rate: float
    profit_factor: float
    avg_trade: float
    exposure: float            # fraction of bars holding a position
    currency: str

    def report(self) -> str:
        cur = self.currency
        return "\n".join([
            f"  bars              {self.bars:>14,}   over {self.span_years:.1f} years",
            f"  net P&L           {self.net_pnl:>14,.0f} {cur}",
            f"  gross P&L         {self.gross_pnl:>14,.0f} {cur}",
            f"  costs             {self.costs:>14,.0f} {cur}"
            + (f"   ({self.cost_share:.0%} of gross)" if self.cost_share == self.cost_share else ""),
            f"  Sharpe            {self.sharpe:>14.2f}",
            f"  Sortino           {self.sortino:>14.2f}",
            f"  max drawdown      {self.max_drawdown:>14,.0f} {cur}",
            f"  trades            {self.trades:>14,}",
            f"  win rate          {self.win_rate:>13.1%}",
            f"  profit factor     {self.profit_factor:>14.2f}",
            f"  avg trade         {self.avg_trade:>14,.2f} {cur}",
            f"  exposure          {self.exposure:>13.1%}",
        ])


def _annualisation(index: pd.DatetimeIndex, bars: int) -> float:
    """Bars per year, from the sample's own clock rather than an assumed calendar.

    Intraday bar counts vary with session length and holidays, so counting the
    actual bars over the actual elapsed time is more honest than 252 x N.
    """
    span = (index[-1] - index[0]).total_seconds()
    if span <= 0:
        return 0.0
    return bars / (span / SECONDS_PER_YEAR)


def compute(result: Result) -> Stats:
    net = result.net_pnl.to_numpy(dtype=float)
    equity = result.equity.to_numpy(dtype=float)
    index = result.equity.index
    bars = len(net)

    per_year = _annualisation(index, bars)
    span_years = bars / per_year if per_year else 0.0

    std = net.std(ddof=1) if bars > 1 else 0.0
    sharpe = float(net.mean() / std * np.sqrt(per_year)) if std > 0 else 0.0

    downside = net[net < 0]
    dstd = downside.std(ddof=1) if len(downside) > 1 else 0.0
    sortino = float(net.mean() / dstd * np.sqrt(per_year)) if dstd > 0 else 0.0

    peak = np.maximum.accumulate(equity)
    drawdown = peak - equity
    max_dd = float(drawdown.max()) if bars else 0.0
    positive_peak = peak[peak > 0]
    max_dd_pct = (
        float((drawdown[peak > 0] / positive_peak).max()) if positive_peak.size else 0.0
    )

    pnls = np.array([t.pnl for t in result.trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    gross = float(result.gross_pnl.sum())
    costs = float(result.costs.sum())

    return Stats(
        bars=bars,
        span_years=span_years,
        net_pnl=float(net.sum()),
        gross_pnl=gross,
        costs=costs,
        cost_share=(costs / gross) if gross > 0 else float("nan"),
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd_pct,
        trades=len(pnls),
        win_rate=float(len(wins) / len(pnls)) if len(pnls) else 0.0,
        profit_factor=(
            float(wins.sum() / -losses.sum()) if losses.size and losses.sum() else float("inf")
        ),
        avg_trade=float(pnls.mean()) if len(pnls) else 0.0,
        exposure=float((result.position != 0).mean()),
        currency=result.contract.currency,
    )
