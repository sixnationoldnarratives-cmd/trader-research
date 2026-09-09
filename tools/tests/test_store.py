import datetime as dt
import os
import shutil
import tempfile
import unittest

from tools.cfd.cli import _planned_days
from tools.cfd.store import TF_1M, TF_8M, Store, aggregate_rows

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HAS_BAR_DATA = os.path.isdir(os.path.join(REPO_ROOT, "bar-data", TF_1M))


def bar(open_ms, o="1.1", h="1.2", lo="1.0", c="1.15", vol="100", src="1"):
    return [str(open_ms), str(open_ms + 59_000), o, h, lo, c, vol, src]


class AggregateTest(unittest.TestCase):
    def test_emits_only_complete_buckets(self):
        base = 1767571200000  # 8M-aligned
        rows = [bar(base + i * 60_000) for i in range(8)] + [bar(base + 8 * 60_000)]
        out = aggregate_rows(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], str(base))
        self.assertEqual(out[0][1], str(base + 479_000))
        self.assertEqual(out[0][7], "8")

    def test_partial_leading_bucket_is_dropped(self):
        base = 1767571200000 + 60_000  # not 8M-aligned
        self.assertEqual(aggregate_rows([bar(base + i * 60_000) for i in range(8)]), [])

    def test_ohlc_and_volume_are_folded_numerically(self):
        base = 1767571200000
        rows = [bar(base + i * 60_000, o=f"1.{i}", h=f"1.{i}5", lo=f"0.{9 - i}",
                    c=f"1.{i}2", vol=str(10 * (i + 1))) for i in range(8)]
        out = aggregate_rows(rows)[0]
        self.assertEqual(out[2], "1.0")     # first open
        self.assertEqual(out[3], "1.75")    # highest high
        self.assertEqual(out[4], "0.2")     # lowest low
        self.assertEqual(out[5], "1.72")    # last close
        self.assertEqual(out[6], str(sum(10 * (i + 1) for i in range(8))))

    def test_high_low_compare_numerically_not_lexicographically(self):
        base = 1767571200000
        rows = [bar(base + i * 60_000, h="9.5", lo="9.5") for i in range(8)]
        rows[3] = bar(base + 3 * 60_000, h="10.5", lo="1.5")
        out = aggregate_rows(rows)[0]
        self.assertEqual(out[3], "10.5")  # lexicographic max would pick "9.5"
        self.assertEqual(out[4], "1.5")   # lexicographic min would pick "1.5" too


@unittest.skipUnless(HAS_BAR_DATA, "bar-data tree not present")
class AggregateAgainstCommittedDataTest(unittest.TestCase):
    """The 8M rule must reproduce the committed files byte for byte."""

    CASES = [("EURUSD", "2015-01"), ("XAUUSD", "2020-03"), ("GBPJPY", "2008-10"),
             ("GBPUSD", "2004-07"), ("AUDNZD", "2019-11")]

    def test_regenerates_committed_8m_months(self):
        store = Store(REPO_ROOT)
        for symbol, month in self.CASES:
            with self.subTest(symbol=symbol, month=month):
                expected = store.read_month(symbol, month, TF_8M)
                self.assertTrue(expected, f"no committed 8M data for {symbol} {month}")
                got = aggregate_rows(store.read_month(symbol, month, TF_1M))
                self.assertEqual(got, expected)


class StoreMutationTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.store = Store(self.root)
        self.base = 1767571200000

    def test_merge_splits_rows_across_months_and_dedupes(self):
        jan = self.base
        feb = 1769990400000  # 2026-02-02T00:00:00Z
        self.store.merge_rows("TEST", [bar(jan, o="1.0"), bar(feb)], TF_1M)
        self.store.merge_rows("TEST", [bar(jan, o="9.9")], TF_1M)
        self.assertEqual(self.store.months("TEST", TF_1M), ["2026-01", "2026-02"])
        rows = self.store.read_month("TEST", "2026-01", TF_1M)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], "9.9")  # newest write wins

    def test_written_file_has_header_and_sorted_rows(self):
        self.store.merge_rows(
            "TEST", [bar(self.base + 120_000), bar(self.base)], TF_1M
        )
        with open(self.store.month_path("TEST", "2026-01", TF_1M)) as handle:
            lines = handle.read().splitlines()
        self.assertTrue(lines[0].startswith("openEpochMillis,closeEpochMillis"))
        self.assertEqual(lines[1].split(",")[0], str(self.base))
        self.assertEqual(lines[2].split(",")[0], str(self.base + 120_000))

    def test_merge_repairs_folds_day_files_and_removes_them(self):
        self.store.merge_rows("TEST", [bar(self.base)], TF_1M)
        directory = self.store.symbol_dir("TEST", TF_1M)
        with open(os.path.join(directory, "__repair_2026-01-05.csv"), "w") as handle:
            handle.write("openEpochMillis,closeEpochMillis,open,high,low,close,volume,sourceBars\n")
            handle.write(",".join(bar(self.base + 60_000)) + "\n")
        count, touched = self.store.merge_repairs("TEST", TF_1M)
        self.assertEqual((count, touched), (1, ["2026-01"]))
        self.assertEqual(len(self.store.read_month("TEST", "2026-01", TF_1M)), 2)
        self.assertEqual(self.store.repair_files("TEST", TF_1M), [])

    def test_rebuild_8m_writes_only_months_with_complete_buckets(self):
        rows = [bar(self.base + i * 60_000) for i in range(8)]
        self.store.merge_rows("TEST", rows, TF_1M)
        self.assertEqual(self.store.rebuild_8m("TEST"), ["2026-01"])
        self.assertEqual(len(self.store.read_month("TEST", "2026-01", TF_8M)), 1)

    def test_coverage_reports_interior_month_gaps(self):
        self.store.merge_rows("TEST", [bar(self.base)], TF_1M)          # 2026-01
        self.store.merge_rows("TEST", [bar(1772409600000)], TF_1M)      # 2026-03
        cov = self.store.coverage("TEST", TF_1M)
        self.assertEqual(cov.missing_months, ["2026-02"])
        self.assertEqual(cov.first_bar_ms, self.base)
        self.assertEqual(cov.last_bar_ms, 1772409600000)

    def test_row_count_is_opt_in_so_status_stays_cheap(self):
        self.store.merge_rows("TEST", [bar(self.base), bar(self.base + 60_000)], TF_1M)
        self.assertEqual(self.store.coverage("TEST", TF_1M).row_count, 0)
        counted = self.store.coverage("TEST", TF_1M, count_rows=True)
        self.assertEqual(counted.row_count, 2)

    def test_month_span_reads_only_the_file_ends(self):
        rows = [bar(self.base + i * 60_000) for i in range(500)]
        self.store.merge_rows("TEST", rows, TF_1M)
        self.assertEqual(
            self.store.month_span("TEST", "2026-01", TF_1M),
            (self.base, self.base + 499 * 60_000),
        )

    def test_month_span_of_a_header_only_file(self):
        self.store.write_month("TEST", "2026-01", [], TF_1M)
        self.assertEqual(self.store.month_span("TEST", "2026-01", TF_1M), (None, None))

    def test_degenerate_months_separates_empty_from_thin(self):
        self.store.merge_rows("TEST", [bar(self.base)], TF_1M)              # 2026-01
        self.store.write_month("TEST", "2026-02", [], TF_1M)                # header only
        empty, thin = self.store.degenerate_months("TEST", TF_1M)
        self.assertEqual(empty, ["2026-02"])
        self.assertEqual(thin, [("2026-01", 1)])

    def test_populated_month_is_neither_empty_nor_thin(self):
        rows = [bar(self.base + i * 60_000) for i in range(1200)]
        self.store.merge_rows("TEST", rows, TF_1M)
        self.assertEqual(self.store.degenerate_months("TEST", TF_1M), ([], []))

    def test_empty_days_round_trip_through_state_file(self):
        days = {dt.date(2026, 1, 1), dt.date(2026, 12, 25)}
        self.store.record_empty_days("TEST", days)
        self.assertEqual(self.store.empty_days("TEST"), days)


class PlannedDaysTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.store = Store(self.root)

    def test_skips_saturdays_present_days_and_known_holidays(self):
        # 2026-01-05 is a Monday; the window runs Mon..Sun.
        self.store.merge_rows("TEST", [bar(1767571200000)], TF_1M)  # Mon 2026-01-05
        self.store.record_empty_days("TEST", {dt.date(2026, 1, 6)})
        days = _planned_days(
            self.store, "TEST", dt.date(2026, 1, 5), dt.date(2026, 1, 11), refetch=False
        )
        self.assertNotIn(dt.date(2026, 1, 5), days)   # already stored
        self.assertNotIn(dt.date(2026, 1, 6), days)   # known empty
        self.assertNotIn(dt.date(2026, 1, 10), days)  # Saturday
        self.assertIn(dt.date(2026, 1, 11), days)     # Sunday: market reopens 21:00
        self.assertEqual(days, [dt.date(2026, 1, d) for d in (7, 8, 9, 11)])

    def test_from_scratch_replans_days_already_stored(self):
        self.store.merge_rows("TEST", [bar(1767571200000)], TF_1M)
        days = _planned_days(
            self.store, "TEST", dt.date(2026, 1, 5), dt.date(2026, 1, 5), refetch=True
        )
        self.assertEqual(days, [dt.date(2026, 1, 5)])


if __name__ == "__main__":
    unittest.main()
