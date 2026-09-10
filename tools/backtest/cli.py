"""``python3 -m tools.backtest`` -- run a strategy over the bar-data tree."""

from __future__ import annotations

import argparse
import datetime as dt
import inspect
import os

import pandas as pd

from tools.backtest import data, metrics, strategies
from tools.backtest.contracts import CONTRACTS, get
from tools.backtest.engine import run as simulate

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

QUALITY_NOTE = {
    "good": "This CFD stands in for the contract well.",
    "intraday-only": "PROXY WARNING -- intraday only.",
    "poor": "PROXY WARNING -- this series is a poor stand-in for the contract.",
}


def _date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


def _strategy_kwargs(func, args) -> dict:
    """Pass through only the parameters this strategy actually declares."""
    accepted = set(inspect.signature(func).parameters) - {"df"}
    supplied = {"fast": args.fast, "slow": args.slow,
                "lookback": args.lookback, "contracts": args.contracts}
    return {k: v for k, v in supplied.items() if k in accepted and v is not None}


def cmd_run(args) -> int:
    contract = get(args.contract)
    strategy = strategies.REGISTRY[args.strategy]

    bars = data.load(
        contract.proxy,
        start=_date(args.start),
        end=_date(args.end),
        root=args.root,
        drop_padding=not args.keep_padding,
    )
    if args.resample:
        bars = data.resample(bars, args.resample)
    if args.session:
        start_hhmm, end_hhmm = args.session.split("-")
        bars = data.session_filter(bars, start_hhmm, end_hhmm)
    if len(bars) < 2:
        raise SystemExit("not enough bars after filtering to simulate anything")

    target = strategy(bars, **_strategy_kwargs(strategy, args))
    result = simulate(
        bars, target, contract,
        commission=args.commission, slippage_ticks=args.slippage_ticks,
    )
    stats = metrics.compute(result)

    print(f"\n{contract.symbol}  {contract.name}")
    print(f"  proxy           {contract.proxy}  ({args.resample or '1m'} bars, "
          f"{bars.index[0].date()} .. {bars.index[-1].date()})")
    print(f"  strategy        {args.strategy} "
          f"{_strategy_kwargs(strategy, args) or ''}")
    print(f"  cost/contract   {contract.cost_per_contract_traded():.2f} "
          f"{contract.currency} per side "
          f"(tick {contract.tick} = {contract.tick_value:.2f})")
    print()
    print(stats.report())

    note = QUALITY_NOTE[contract.proxy_quality]
    print(f"\n{note}\n  {contract.caveat}")
    if contract.proxy_quality != "good":
        overnight = _overnight_share(result.position)
        if overnight > 0.05:
            print(f"  {overnight:.0%} of held bars span a session gap, which is "
                  f"exactly where this proxy diverges. Treat the result as a "
                  f"plumbing check, not a finding.")
    return 0


def _overnight_share(position: pd.Series) -> float:
    """Fraction of held bars whose gap to the previous bar exceeds ten minutes."""
    held = position != 0
    if not held.any():
        return 0.0
    gaps = position.index.to_series().diff() > pd.Timedelta(minutes=10)
    return float((held & gaps).sum() / held.sum())


def cmd_contracts(args) -> int:
    print(f"{'sym':<6} {'contract':<32} {'proxy':<15} {'tick value':>10}  quality")
    for symbol in sorted(CONTRACTS):
        c = CONTRACTS[symbol]
        print(f"{c.symbol:<6} {c.name:<32} {c.proxy:<15} "
              f"{c.tick_value:>7.2f} {c.currency}  {c.proxy_quality}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m tools.backtest",
        description="Backtest a strategy against the bar-data tree, with futures economics.",
    )
    parser.add_argument("--root", default=REPO_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="simulate a strategy")
    p.add_argument("--contract", required=True, help="e.g. GC, NQ, CL, FDAX")
    p.add_argument("--strategy", default="ma_cross", choices=sorted(strategies.REGISTRY))
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--resample", help="coarser bars, e.g. 5min, 1h, 1D")
    p.add_argument("--session", help="restrict to a UTC window, e.g. 13:30-20:00")
    p.add_argument("--contracts", type=float, default=1.0)
    p.add_argument("--fast", type=int)
    p.add_argument("--slow", type=int)
    p.add_argument("--lookback", type=int)
    p.add_argument("--commission", type=float)
    p.add_argument("--slippage-ticks", type=float, dest="slippage_ticks")
    p.add_argument("--keep-padding", action="store_true",
                   help="keep market-closed filler bars (they deflate volatility)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("contracts", help="list known contracts and their proxies")
    p.set_defaults(func=cmd_contracts)

    args = parser.parse_args(argv)
    return args.func(args)
