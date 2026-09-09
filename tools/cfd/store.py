"""Read/write access to the bar-data tree.

Layout::

    bar-data/{1M,8M}/DUKASCOPY_BID/{SYMBOL}/{YYYY-MM}.csv

Both timeframes carry the header
``openEpochMillis,closeEpochMillis,open,high,low,close,volume,sourceBars``.

The 8M tree is derived locally from 1M, never downloaded: bars are bucketed onto
an absolute 480000 ms grid, and a bucket is emitted only when all eight
constituent minutes are present (``sourceBars`` is 8 in every existing row).
Because 480000 divides a day evenly, buckets never straddle a day or month
boundary.  Regenerating 2015-01 EURUSD, 2020-03 XAUUSD and 2008-10 GBPJPY with
:func:`aggregate_rows` reproduces the committed files exactly.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import tempfile
from dataclasses import dataclass

SOURCE = "DUKASCOPY_BID"
TF_1M = "1M"
TF_8M = "8M"
HEADER = [
    "openEpochMillis",
    "closeEpochMillis",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "sourceBars",
]

AGG_FACTOR = 8
AGG_BUCKET_MS = 480_000
AGG_CLOSE_OFFSET_MS = 479_000

EPOCH_DAY = dt.date(1970, 1, 1)

MONTH_RE = re.compile(r"^(\d{4})-(\d{2})\.csv$")
REPAIR_RE = re.compile(r"^__repair_(\d{4})-(\d{2})-(\d{2})\.csv$")

Row = list[str]


def month_key(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m")


def day_key(ms: int) -> dt.date:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).date()


def month_bounds(month: str) -> tuple[dt.date, dt.date]:
    year, mon = (int(p) for p in month.split("-"))
    first = dt.date(year, mon, 1)
    last = dt.date(year + (mon == 12), (mon % 12) + 1, 1) - dt.timedelta(days=1)
    return first, last


def months_between(start: dt.date, end: dt.date) -> list[str]:
    """Month keys touched by the inclusive date range."""
    out, year, mon = [], start.year, start.month
    while (year, mon) <= (end.year, end.month):
        out.append(f"{year:04d}-{mon:02d}")
        year, mon = (year + 1, 1) if mon == 12 else (year, mon + 1)
    return out


def aggregate_rows(rows: list[Row]) -> list[Row]:
    """Fold 1M rows into complete 8M buckets, preserving source number formatting."""
    buckets: dict[int, list[Row]] = {}
    for row in rows:
        buckets.setdefault(int(row[0]) // AGG_BUCKET_MS * AGG_BUCKET_MS, []).append(row)

    out: list[Row] = []
    for start in sorted(buckets):
        group = buckets[start]
        if len(group) != AGG_FACTOR:
            continue
        group.sort(key=lambda r: int(r[0]))
        out.append(
            [
                str(start),
                str(start + AGG_CLOSE_OFFSET_MS),
                group[0][2],
                max(group, key=lambda r: float(r[3]))[3],
                min(group, key=lambda r: float(r[4]))[4],
                group[-1][5],
                str(sum(int(r[6]) for r in group)),
                str(AGG_FACTOR),
            ]
        )
    return out


#: A month file smaller than this holds at most a few hundred bars, so it is
#: worth counting its rows exactly; anything larger is unambiguously populated.
THIN_FILE_BYTES = 80_000
#: Below this many bars a month is reported as thin rather than covered.
THIN_MONTH_ROWS = 1_000


@dataclass
class Coverage:
    symbol: str
    months: list[str]
    missing_months: list[str]
    first_bar_ms: int | None
    last_bar_ms: int | None
    row_count: int
    repair_files: list[str]
    empty_months: list[str]
    thin_months: list[tuple[str, int]]

    @property
    def first_day(self) -> dt.date | None:
        return day_key(self.first_bar_ms) if self.first_bar_ms is not None else None

    @property
    def last_day(self) -> dt.date | None:
        return day_key(self.last_bar_ms) if self.last_bar_ms is not None else None


class Store:
    def __init__(self, root: str | os.PathLike):
        self.root = os.fspath(root)
        self.bar_data = os.path.join(self.root, "bar-data")
        self.state_path = os.path.join(self.bar_data, "_download_state.json")

    # ---- paths -----------------------------------------------------------
    def symbol_dir(self, symbol: str, timeframe: str = TF_1M) -> str:
        return os.path.join(self.bar_data, timeframe, SOURCE, symbol)

    def month_path(self, symbol: str, month: str, timeframe: str = TF_1M) -> str:
        return os.path.join(self.symbol_dir(symbol, timeframe), f"{month}.csv")

    def symbols(self, timeframe: str = TF_1M) -> list[str]:
        base = os.path.join(self.bar_data, timeframe, SOURCE)
        if not os.path.isdir(base):
            return []
        return sorted(e for e in os.listdir(base) if os.path.isdir(os.path.join(base, e)))

    def months(self, symbol: str, timeframe: str = TF_1M) -> list[str]:
        directory = self.symbol_dir(symbol, timeframe)
        if not os.path.isdir(directory):
            return []
        return sorted(
            name[:-4] for name in os.listdir(directory) if MONTH_RE.match(name)
        )

    def repair_files(self, symbol: str, timeframe: str = TF_1M) -> list[str]:
        directory = self.symbol_dir(symbol, timeframe)
        if not os.path.isdir(directory):
            return []
        return sorted(name for name in os.listdir(directory) if REPAIR_RE.match(name))

    # ---- csv io ----------------------------------------------------------
    def read_rows(self, path: str) -> list[Row]:
        if not os.path.exists(path):
            return []
        with open(path, newline="") as handle:
            reader = csv.reader(handle)
            rows = [row for row in reader if row]
        if rows and rows[0][0] == HEADER[0]:
            rows = rows[1:]
        return rows

    def read_month(self, symbol: str, month: str, timeframe: str = TF_1M) -> list[Row]:
        return self.read_rows(self.month_path(symbol, month, timeframe))

    def write_month(
        self, symbol: str, month: str, rows: list[Row], timeframe: str = TF_1M
    ) -> None:
        path = self.month_path(symbol, month, timeframe)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", newline="", delete=False, dir=os.path.dirname(path), suffix=".tmp"
        )
        try:
            with handle:
                writer = csv.writer(handle, lineterminator="\n")
                writer.writerow(HEADER)
                writer.writerows(rows)
            os.replace(handle.name, path)
        except BaseException:
            os.unlink(handle.name)
            raise

    # ---- coverage --------------------------------------------------------
    def _first_last_line(self, path: str) -> tuple[bytes | None, bytes | None]:
        """First and last data lines of a month file without reading the middle."""
        with open(path, "rb") as handle:
            handle.readline()  # header
            first = handle.readline().strip()
            if not first:
                return None, None
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - 4096))
            tail = handle.read().splitlines()
        last = next((line for line in reversed(tail) if line.strip()), first)
        return first, last.strip()

    def month_span(self, symbol: str, month: str, timeframe: str = TF_1M):
        """``(first_open_ms, last_open_ms)`` for a month, or ``(None, None)``."""
        path = self.month_path(symbol, month, timeframe)
        if not os.path.exists(path):
            return None, None
        first, last = self._first_last_line(path)
        if first is None:
            return None, None
        return int(first.split(b",", 1)[0]), int(last.split(b",", 1)[0])

    def coverage(
        self, symbol: str, timeframe: str = TF_1M, *, count_rows: bool = False
    ) -> Coverage:
        months = self.months(symbol, timeframe)
        missing: list[str] = []
        if months:
            cursor, end = months[0], months[-1]
            present = set(months)
            while cursor <= end:
                if cursor not in present:
                    missing.append(cursor)
                year, mon = (int(p) for p in cursor.split("-"))
                year, mon = (year + 1, 1) if mon == 12 else (year, mon + 1)
                cursor = f"{year:04d}-{mon:02d}"

        first_ms = last_ms = None
        for month in months:
            first_ms = self.month_span(symbol, month, timeframe)[0]
            if first_ms is not None:
                break
        for month in reversed(months):
            last_ms = self.month_span(symbol, month, timeframe)[1]
            if last_ms is not None:
                break

        total = 0
        if count_rows:
            for month in months:
                total += len(self.read_month(symbol, month, timeframe))
        empty, thin = self.degenerate_months(symbol, timeframe)
        return Coverage(
            symbol=symbol,
            months=months,
            missing_months=missing,
            first_bar_ms=first_ms,
            last_bar_ms=last_ms,
            row_count=total,
            repair_files=self.repair_files(symbol, timeframe),
            empty_months=empty,
            thin_months=thin,
        )

    def degenerate_months(
        self, symbol: str, timeframe: str = TF_1M
    ) -> tuple[list[str], list[tuple[str, int]]]:
        """Months whose file exists but holds no (or barely any) data.

        A present ``YYYY-MM.csv`` is not the same as a covered month: the tree
        contains header-only files and months with a few hundred bars where a
        full month holds around thirty thousand.
        """
        empty: list[str] = []
        thin: list[tuple[str, int]] = []
        for month in self.months(symbol, timeframe):
            path = self.month_path(symbol, month, timeframe)
            size = os.path.getsize(path)
            if size <= len(",".join(HEADER)) + 2:
                empty.append(month)
            elif size < THIN_FILE_BYTES:
                with open(path, "rb") as handle:
                    rows = sum(1 for _ in handle) - 1
                if rows < THIN_MONTH_ROWS:
                    thin.append((month, rows))
        return empty, thin

    def days_present(
        self, symbol: str, timeframe: str = TF_1M, months: list[str] | None = None
    ) -> set[dt.date]:
        """Days that already hold at least one bar.

        Scans only the requested months -- reading a symbol's whole history to
        plan a few days of catch-up costs hundreds of megabytes for nothing.
        """
        wanted = self.months(symbol, timeframe) if months is None else months
        days: set[dt.date] = set()
        for month in wanted:
            path = self.month_path(symbol, month, timeframe)
            if not os.path.exists(path):
                continue
            with open(path, "rb") as handle:
                handle.readline()  # header
                for line in handle:
                    comma = line.find(b",")
                    if comma > 0:
                        days.add(EPOCH_DAY + dt.timedelta(days=int(line[:comma]) // 86_400_000))
        return days

    # ---- mutation --------------------------------------------------------
    def merge_rows(self, symbol: str, rows: list[Row], timeframe: str = TF_1M) -> list[str]:
        """Merge rows into their monthly files; new rows win on timestamp collision."""
        by_month: dict[str, list[Row]] = {}
        for row in rows:
            by_month.setdefault(month_key(int(row[0])), []).append(row)

        touched = []
        for month, incoming in sorted(by_month.items()):
            merged = {int(r[0]): r for r in self.read_month(symbol, month, timeframe)}
            merged.update({int(r[0]): r for r in incoming})
            self.write_month(
                symbol, month, [merged[k] for k in sorted(merged)], timeframe
            )
            touched.append(month)
        return touched

    def rebuild_8m(self, symbol: str, months: list[str] | None = None) -> list[str]:
        """Regenerate 8M months from the 1M tree."""
        targets = months if months is not None else self.months(symbol, TF_1M)
        written = []
        for month in targets:
            rows = aggregate_rows(self.read_month(symbol, month, TF_1M))
            if rows:
                self.write_month(symbol, month, rows, TF_8M)
                written.append(month)
        return written

    def merge_repairs(self, symbol: str, timeframe: str = TF_1M) -> tuple[int, list[str]]:
        """Fold ``__repair_YYYY-MM-DD.csv`` day files into their monthly files."""
        directory = self.symbol_dir(symbol, timeframe)
        names = self.repair_files(symbol, timeframe)
        rows: list[Row] = []
        for name in names:
            rows.extend(self.read_rows(os.path.join(directory, name)))
        if not rows:
            return 0, []
        touched = self.merge_rows(symbol, rows, timeframe)
        for name in names:
            os.unlink(os.path.join(directory, name))
        return len(rows), touched

    # ---- state -----------------------------------------------------------
    def load_state(self) -> dict:
        if not os.path.exists(self.state_path):
            return {}
        with open(self.state_path) as handle:
            return json.load(handle)

    def save_state(self, state: dict) -> None:
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w") as handle:
            json.dump(state, handle, indent=1, sort_keys=True)
            handle.write("\n")

    def empty_days(self, symbol: str) -> set[dt.date]:
        raw = self.load_state().get("empty_days", {}).get(symbol, [])
        return {dt.date.fromisoformat(d) for d in raw}

    def record_empty_days(self, symbol: str, days: set[dt.date]) -> None:
        if not days:
            return
        state = self.load_state()
        bucket = state.setdefault("empty_days", {})
        merged = set(bucket.get(symbol, [])) | {d.isoformat() for d in days}
        bucket[symbol] = sorted(merged)
        self.save_state(state)
