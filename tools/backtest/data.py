"""Loading bar-data into DataFrames, and resampling to a working timeframe.

One-minute bars over twenty-odd years is around nine million rows per symbol.
Loading only the months a backtest asks for, and resampling once up front, is
the difference between a run that takes seconds and one that takes minutes.
"""

from __future__ import annotations

import datetime as dt
import os

import pandas as pd

from tools.cfd.store import TF_1M, Store

COLUMNS = ["open", "high", "low", "close", "volume"]
_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
_DTYPE = {
    "openEpochMillis": "int64",
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",
    "volume": "int64",
}


def _months_in_range(store: Store, symbol: str, timeframe: str,
                     start: dt.date | None, end: dt.date | None) -> list[str]:
    months = store.months(symbol, timeframe)
    if start:
        months = [m for m in months if m >= start.strftime("%Y-%m")]
    if end:
        months = [m for m in months if m <= end.strftime("%Y-%m")]
    return months


def is_padding(df: pd.DataFrame) -> pd.Series:
    """Bars the feed emitted while the market was shut.

    Flat OHLC *and* zero volume: no trade happened, the price was carried
    forward. Around 7% of XAUUSD's minutes are these, clustered in the daily
    break and over weekends. Left in a backtest they add long runs of zero
    return, which deflates measured volatility, flattens moving averages and
    ATR, and invents signals at the boundary where quoting resumes.

    Zero volume with a price that moved is *not* counted here -- that is a
    quote the feed recorded without size, and dropping it would discard real
    price action.
    """
    flat = (df["open"] == df["high"]) & (df["high"] == df["low"]) & (df["low"] == df["close"])
    return flat & (df["volume"] == 0)


def load(
    symbol: str,
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
    timeframe: str = TF_1M,
    root: str = ".",
    drop_padding: bool = True,
) -> pd.DataFrame:
    """Bars for ``symbol`` as a UTC-indexed OHLCV frame.

    ``drop_padding`` removes market-closed filler bars; see :func:`is_padding`.
    Pass ``False`` only to inspect the raw feed.
    """
    store = Store(root)
    months = _months_in_range(store, symbol, timeframe, start, end)
    if not months:
        raise FileNotFoundError(
            f"no {timeframe} data for {symbol} in {start}..{end}. "
            f"Available: {', '.join(store.months(symbol, timeframe)[:1] or ['none'])}"
            f"..{''.join(store.months(symbol, timeframe)[-1:] or [''])}"
        )

    frames = []
    for month in months:
        path = store.month_path(symbol, month, timeframe)
        if not os.path.exists(path) or os.path.getsize(path) <= 80:
            continue
        frames.append(
            pd.read_csv(
                path,
                usecols=list(_DTYPE),
                dtype=_DTYPE,
                engine="c",
            )
        )
    if not frames:
        raise FileNotFoundError(f"every month file for {symbol} in range is empty")

    df = pd.concat(frames, ignore_index=True)
    df.index = pd.to_datetime(df.pop("openEpochMillis"), unit="ms", utc=True)
    df.index.name = "timestamp"
    df = df[COLUMNS].sort_index()
    df = df[~df.index.duplicated(keep="last")]

    if start:
        df = df[df.index >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df[df.index < pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)]
    if drop_padding:
        df = df[~is_padding(df)]
    return df


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate to a coarser bar. Empty periods (weekends, holidays) are dropped."""
    out = df.resample(rule, label="left", closed="left").agg(_AGG)
    return out.dropna(subset=["open"])


def session_filter(df: pd.DataFrame, start_hhmm: str, end_hhmm: str) -> pd.DataFrame:
    """Keep only bars whose UTC time falls in [start, end).

    Futures trade defined sessions; a CFD series does not stop where the
    contract does, so restricting the window is usually closer to the truth
    than testing across hours the contract was not trading.
    """
    start_t = dt.time.fromisoformat(start_hhmm)
    end_t = dt.time.fromisoformat(end_hhmm)
    times = df.index.time
    keep = ((times >= start_t) & (times < end_t)) if start_t <= end_t else \
           ((times >= start_t) | (times < end_t))
    return df[keep]
