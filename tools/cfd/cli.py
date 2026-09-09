"""Command line entry point: ``python3 -m tools.cfd <command>``."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

from tools.cfd import instruments
from tools.cfd.dukascopy import EgressBlocked, FetchError, SanityError, download_day
from tools.cfd.store import TF_1M, TF_8M, Store, month_key, months_between

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _today() -> dt.date:
    return dt.datetime.now(tz=dt.timezone.utc).date()


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _resolve_symbols(store: Store, requested: list[str] | None) -> list[str]:
    known = store.symbols(TF_1M)
    if not requested:
        return known
    missing = [s for s in requested if s not in known and s not in instruments.INSTRUMENTS]
    if missing:
        raise SystemExit(f"unknown symbol(s): {', '.join(missing)}")
    return requested


def _iter_days(start: dt.date, end: dt.date):
    day = start
    while day <= end:
        yield day
        day += dt.timedelta(days=1)


def _planned_days(
    store: Store,
    symbol: str,
    start: dt.date,
    end: dt.date,
    *,
    refetch: bool,
    force: frozenset[dt.date] = frozenset(),
) -> list[dt.date]:
    """Days worth asking the feed for.

    Saturdays are skipped outright -- the market is closed 21:00 Friday to 21:00
    Sunday UTC.  Holidays are not special-cased: the first run discovers them,
    records them in ``bar-data/_download_state.json`` and never asks again.
    Days in ``force`` are planned even if bars already exist, which is how the
    last stored day gets topped up when a previous run stopped mid-day.
    """
    if start > end:
        return []
    window = months_between(start, end)
    present = set() if refetch else store.days_present(symbol, TF_1M, window)
    known_empty = store.empty_days(symbol)
    return [
        day
        for day in _iter_days(start, end)
        if day.weekday() != 5
        and (day in force or (day not in present and day not in known_empty))
    ]


# ---------------------------------------------------------------- status ----
def cmd_status(args, store: Store) -> int:
    today = _today()
    symbols = _resolve_symbols(store, args.symbols)
    print(
        f"{'symbol':<15} {'range':<18} {'months':>13} {'last bar':<12} "
        f"{'stale':>6}  notes"
    )
    for symbol in symbols:
        cov = store.coverage(symbol, TF_1M)
        if not cov.months:
            print(f"{symbol:<15} {'(empty)':<18}")
            continue
        expected = len(cov.months) + len(cov.missing_months)
        stale = (today - cov.last_day).days if cov.last_day else None
        notes = []
        if cov.missing_months:
            notes.append(_plural(len(cov.missing_months), "month") + " absent")
        if cov.empty_months:
            notes.append(_plural(len(cov.empty_months), "empty month file"))
        if cov.thin_months:
            thinnest = min(cov.thin_months, key=lambda item: item[1])
            notes.append(
                f"{_plural(len(cov.thin_months), 'thin month')} "
                f"(e.g. {thinnest[0]}: {thinnest[1]} bars)"
            )
        if cov.repair_files:
            notes.append(_plural(len(cov.repair_files), "unmerged __repair_ file"))
        if symbol in instruments.SUSPECT_SYMBOLS:
            notes.append("SUSPECT: values are not real quotes, refetch --from-scratch")
        eight = store.coverage(symbol, TF_8M)
        if eight.months:
            # Empty and thin 1M months may hold no complete eight-minute
            # bucket at all, so their absence from 8M is not a gap.
            expected_8m = (
                set(cov.months)
                - set(cov.empty_months)
                - {month for month, _ in cov.thin_months}
            )
            behind = sorted(expected_8m - set(eight.months))
            if behind:
                notes.append(
                    f"{_plural(len(behind), 'month')} missing from 8M "
                    f"(run aggregate)"
                )
        elif symbol in store.symbols(TF_1M):
            notes.append("no 8M tree")
        print(
            f"{symbol:<15} {cov.months[0]}..{cov.months[-1]}  "
            f"{len(cov.months):>5}/{expected:<7} {str(cov.last_day):<12} "
            f"{stale:>4}d  {'; '.join(notes)}"
        )
    return 0


# -------------------------------------------------------------- download ----
def cmd_download(args, store: Store) -> int:
    today = _today()
    end = dt.date.fromisoformat(args.end) if args.end else today - dt.timedelta(days=1)
    symbols = _resolve_symbols(store, args.symbols)
    failures = 0

    for symbol in symbols:
        inst = instruments.get(symbol)
        cov = store.coverage(symbol, TF_1M)

        if args.from_scratch and not args.dry_run:
            for timeframe in (TF_1M, TF_8M):
                directory = store.symbol_dir(symbol, timeframe)
                if os.path.isdir(directory):
                    shutil.rmtree(directory)
            state = store.load_state()
            state.get("empty_days", {}).pop(symbol, None)
            store.save_state(state)

        if args.start:
            start = dt.date.fromisoformat(args.start)
        elif args.from_scratch or args.backfill:
            start = cov.first_day
        else:
            # Catch-up only: resume at the last stored day, which may itself be
            # partial if an earlier run stopped part-way through it.
            start = cov.last_day
        if start is None:
            print(f"{symbol}: no existing data and no --start given; skipping")
            continue

        force = frozenset([cov.last_day]) if cov.last_day and not args.from_scratch else frozenset()
        days = _planned_days(
            store, symbol, start, end, refetch=args.from_scratch, force=force
        )
        if not days:
            print(f"{symbol}: up to date through {end}")
            continue

        print(f"{symbol}: {len(days)} day(s) to fetch, {start}..{end}")
        if args.dry_run:
            continue

        by_month: dict[str, list[dt.date]] = {}
        for day in days:
            by_month.setdefault(day.strftime("%Y-%m"), []).append(day)

        symbol_failed = False
        for month, month_days in sorted(by_month.items()):
            if symbol_failed:
                break
            rows, empty = [], set()
            with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                futures = {
                    pool.submit(download_day, symbol, day, inst): day
                    for day in month_days
                }
                for future in futures:
                    day = futures[future]
                    try:
                        bars = future.result()
                    except EgressBlocked as exc:
                        raise SystemExit(f"\n{exc}") from None
                    except SanityError as exc:
                        print(f"  ABORT {exc}", file=sys.stderr)
                        symbol_failed = True
                        failures += 1
                        break
                    except FetchError as exc:
                        print(f"  WARN  {exc}", file=sys.stderr)
                        failures += 1
                        continue
                    if bars:
                        rows.extend(bar.row() for bar in bars)
                    else:
                        empty.add(day)
            if symbol_failed:
                break
            if rows:
                rows.sort(key=lambda r: int(r[0]))
                touched = store.merge_rows(symbol, rows, TF_1M)
                store.rebuild_8m(symbol, touched)
                print(f"  {month}: +{len(rows)} bars -> {', '.join(touched)}")
            store.record_empty_days(symbol, empty)
    return 1 if failures else 0


# ------------------------------------------------------------- aggregate ----
def cmd_aggregate(args, store: Store) -> int:
    for symbol in _resolve_symbols(store, args.symbols):
        written = store.rebuild_8m(symbol)
        print(f"{symbol}: rebuilt {len(written)} 8M month(s)")
    return 0


# --------------------------------------------------------- merge-repairs ----
def cmd_merge_repairs(args, store: Store) -> int:
    for symbol in _resolve_symbols(store, args.symbols):
        names = store.repair_files(symbol, TF_1M)
        if not names:
            continue
        if args.dry_run:
            print(f"{symbol}: would merge {len(names)} file(s): {names[0]}..{names[-1]}")
            continue
        count, touched = store.merge_repairs(symbol, TF_1M)
        store.rebuild_8m(symbol, touched)
        print(
            f"{symbol}: merged {count} bars from {len(names)} repair file(s) "
            f"into {', '.join(touched)}; repair files removed"
        )
    return 0


# ------------------------------------------------------------- verify ------
def cmd_verify(args, store: Store) -> int:
    """Re-download days already in the repo and diff, proving the decode settings.

    This is the check that turns the assumptions in ``instruments.py`` -- digits,
    volume scale, the open/close/low/high record order -- from documentation into
    something tested against 8,848 files of committed ground truth.
    """
    mismatches = 0
    for symbol in _resolve_symbols(store, args.symbols):
        inst = instruments.get(symbol)
        months = store.months(symbol, TF_1M)
        if not months:
            continue
        picks = [months[i * (len(months) - 1) // max(args.days - 1, 1)] for i in range(args.days)]
        for month in dict.fromkeys(picks):
            stored = store.read_month(symbol, month, TF_1M)
            if not stored:
                continue
            day = dt.datetime.fromtimestamp(
                int(stored[0][0]) / 1000, tz=dt.timezone.utc
            ).date()
            expected = [r for r in stored if month_key(int(r[0])) == month
                        and dt.datetime.fromtimestamp(int(r[0]) / 1000, tz=dt.timezone.utc).date() == day]
            try:
                got = [bar.row() for bar in download_day(symbol, day, inst)]
            except EgressBlocked as exc:
                raise SystemExit(f"\n{exc}") from None
            except (SanityError, FetchError) as exc:
                print(f"{symbol} {day}: {exc}")
                mismatches += 1
                continue
            if got == expected:
                print(f"{symbol} {day}: OK ({len(got)} bars identical)")
            else:
                mismatches += 1
                print(f"{symbol} {day}: MISMATCH stored={len(expected)} fetched={len(got)}")
                for stored_row, fetched_row in list(zip(expected, got))[:3]:
                    if stored_row != fetched_row:
                        print(f"    stored : {','.join(stored_row)}")
                        print(f"    fetched: {','.join(fetched_row)}")
    return 1 if mismatches else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m tools.cfd",
        description="Download and maintain the Dukascopy bar-data tree.",
    )
    parser.add_argument("--root", default=REPO_ROOT, help="repo root (default: this checkout)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="coverage audit of the bar-data tree")
    p.add_argument("symbols", nargs="*")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("download", help="fetch missing days and refresh 8M")
    p.add_argument("symbols", nargs="*")
    p.add_argument("--start", help="ISO date; default is each symbol's first stored day")
    p.add_argument("--end", help="ISO date; default is yesterday UTC")
    p.add_argument("--from-scratch", action="store_true",
                   help="delete the symbol's 1M and 8M trees and refetch the full range")
    p.add_argument("--backfill", action="store_true",
                   help="also plan days from the start of history, filling interior gaps")
    p.add_argument("--jobs", type=int, default=4, help="parallel day requests (default 4)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("aggregate", help="rebuild the 8M tree from 1M")
    p.add_argument("symbols", nargs="*")
    p.set_defaults(func=cmd_aggregate)

    p = sub.add_parser("merge-repairs", help="fold __repair_*.csv day files into monthly files")
    p.add_argument("symbols", nargs="*")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_merge_repairs)

    p = sub.add_parser("verify", help="refetch stored days and diff against the repo")
    p.add_argument("symbols", nargs="*")
    p.add_argument("--days", type=int, default=3, help="sample days per symbol (default 3)")
    p.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args, Store(args.root))
