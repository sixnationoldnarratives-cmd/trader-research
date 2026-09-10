"""Example strategies.

These exist so the machinery can be exercised end to end and so a new strategy
has a shape to copy. They are textbook rules with no edge claimed for them --
treat any result they produce as a test of the plumbing, not a finding.

A strategy takes the bar frame and returns the position, in contracts, it wants
to hold *after* each bar closes. The engine applies it from the next bar's open,
so a strategy cannot accidentally trade on information from its own bar.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def flat(df: pd.DataFrame) -> pd.Series:
    """Hold nothing. The control: net P&L must be exactly zero."""
    return pd.Series(0.0, index=df.index)


def buy_and_hold(df: pd.DataFrame, contracts: float = 1.0) -> pd.Series:
    return pd.Series(float(contracts), index=df.index)


def ma_cross(df: pd.DataFrame, fast: int = 20, slow: int = 100,
             contracts: float = 1.0) -> pd.Series:
    if fast >= slow:
        raise ValueError(f"fast ({fast}) must be shorter than slow ({slow})")
    close = df["close"]
    f = close.rolling(fast).mean()
    s = close.rolling(slow).mean()
    signal = np.sign(f - s).fillna(0.0)
    return signal * contracts


def donchian(df: pd.DataFrame, lookback: int = 100, contracts: float = 1.0) -> pd.Series:
    """Long on a new N-bar high, short on a new N-bar low, hold until the other."""
    # shift(1) so the channel is built from bars strictly before the current one
    high = df["high"].rolling(lookback).max().shift(1)
    low = df["low"].rolling(lookback).min().shift(1)
    close = df["close"]
    signal = pd.Series(np.nan, index=df.index)
    signal[close > high] = 1.0
    signal[close < low] = -1.0
    return signal.ffill().fillna(0.0) * contracts


REGISTRY = {
    "flat": flat,
    "buy_and_hold": buy_and_hold,
    "ma_cross": ma_cross,
    "donchian": donchian,
}
