import datetime as dt
import lzma
import struct
import unittest
import urllib.error

from tools.cfd import dukascopy as dk
from tools.cfd import instruments


def encode(records) -> bytes:
    raw = b"".join(dk.CANDLE.pack(*r) for r in records)
    comp = lzma.LZMACompressor(format=lzma.FORMAT_ALONE)
    return comp.compress(raw) + comp.flush()


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def open(self, request, timeout=None):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)


class UrlTest(unittest.TestCase):
    def test_month_is_zero_indexed(self):
        self.assertTrue(
            dk.day_url("EURUSD", dt.date(2026, 1, 3)).endswith(
                "/EURUSD/2026/00/03/BID_candles_min_1.bi5"
            )
        )
        self.assertTrue(
            dk.day_url("XAUUSD", dt.date(2020, 12, 31)).endswith(
                "/XAUUSD/2020/11/31/BID_candles_min_1.bi5"
            )
        )


class FormatTest(unittest.TestCase):
    def test_matches_repo_convention(self):
        # Trailing zeros are stripped in the committed files: 1.15430 -> 1.1543
        self.assertEqual(dk.format_price(1.15430, 5), "1.1543")
        self.assertEqual(dk.format_price(1.15446, 5), "1.15446")
        self.assertEqual(dk.format_price(4081.935, 3), "4081.935")
        self.assertEqual(dk.format_price(-37.63, 3), "-37.63")
        self.assertEqual(dk.format_price(0.0, 5), "0")


class DecodeTest(unittest.TestCase):
    def setUp(self):
        self.eurusd = instruments.get("EURUSD")
        self.day = dt.date(2026, 1, 5)
        self.day_ms = 1767571200000  # 2026-01-05T00:00:00Z

    def test_decodes_records_into_repo_shaped_bars(self):
        blob = encode([
            (0, 115446, 115445, 115443, 115448, 3.6),
            (60, 115447, 115468, 115443, 115472, 9.0),
        ])
        bars = dk.decode_day(blob, self.day, self.eurusd)
        self.assertEqual(len(bars), 2)
        first = bars[0]
        self.assertEqual(first.open_ms, self.day_ms)
        self.assertEqual(first.close_ms, self.day_ms + 59_000)
        # record order is open, close, low, high
        self.assertEqual(first.open, "1.15446")
        self.assertEqual(first.close, "1.15445")
        self.assertEqual(first.low, "1.15443")
        self.assertEqual(first.high, "1.15448")
        self.assertEqual(first.volume, 3_600_000)
        self.assertEqual(first.source_bars, 1)
        self.assertEqual(bars[1].open_ms, self.day_ms + 60_000)

    def test_row_matches_csv_column_order(self):
        blob = encode([(0, 115446, 115445, 115443, 115448, 3.6)])
        row = dk.decode_day(blob, self.day, self.eurusd)[0].row()
        self.assertEqual(
            row,
            [str(self.day_ms), str(self.day_ms + 59_000),
             "1.15446", "1.15448", "1.15443", "1.15445", "3600000", "1"],
        )

    def test_all_zero_padding_records_are_dropped(self):
        blob = encode([(0, 0, 0, 0, 0, 0.0), (60, 115447, 115468, 115443, 115472, 9.0)])
        self.assertEqual(len(dk.decode_day(blob, self.day, self.eurusd)), 1)

    def test_negative_prices_survive(self):
        # WTI settled below zero on 2020-04-20; prices must decode as signed.
        light = instruments.get("LIGHTCMDUSD")
        blob = encode([(0, -37630, -36000, -40000, -35000, 1.0)])
        bar = dk.decode_day(blob, dt.date(2020, 4, 20), light)[0]
        self.assertEqual(bar.open, "-37.63")
        self.assertEqual(bar.low, "-40")

    def test_wrong_scale_is_refused_with_a_hint(self):
        # EURUSD priced as if digits were 3: 115.446 is nowhere near 0.5..2.0
        blob = encode([(0, 115446, 115445, 115443, 115448, 3.6)])
        wrong = instruments.Instrument("EURUSD", 3, 0.5, 2.0)
        with self.assertRaises(dk.SanityError) as ctx:
            dk.decode_day(blob, self.day, wrong)
        self.assertIn("digits=5 would fit", str(ctx.exception))
        self.assertIn("Nothing written", str(ctx.exception))

    def test_truncated_payload_is_rejected(self):
        raw = dk.CANDLE.pack(0, 115446, 115445, 115443, 115448, 3.6)[:-4]
        comp = lzma.LZMACompressor(format=lzma.FORMAT_ALONE)
        with self.assertRaises(ValueError):
            dk.decode_day(comp.compress(raw) + comp.flush(), self.day, self.eurusd)


class FetchTest(unittest.TestCase):
    def _error(self, code):
        return urllib.error.HTTPError("u", code, "m", {}, None)

    def test_404_means_no_data_not_failure(self):
        opener = FakeOpener(self._error(404))
        self.assertIsNone(dk.fetch_day("EURUSD", dt.date(2026, 1, 3), opener=opener))

    def test_empty_body_means_no_data(self):
        opener = FakeOpener(b"")
        self.assertIsNone(dk.fetch_day("EURUSD", dt.date(2026, 1, 3), opener=opener))

    def test_retries_server_errors_then_succeeds(self):
        slept = []
        opener = FakeOpener(self._error(503), self._error(503), b"payload")
        got = dk.fetch_day(
            "EURUSD", dt.date(2026, 1, 3), opener=opener, sleep=slept.append
        )
        self.assertEqual(got, b"payload")
        self.assertEqual(slept, [2.0, 4.0])  # exponential backoff

    def test_gives_up_after_retries(self):
        opener = FakeOpener(*[self._error(500)] * 5)
        with self.assertRaises(dk.FetchError):
            dk.fetch_day(
                "EURUSD", dt.date(2026, 1, 3), opener=opener, sleep=lambda _: None
            )

    def test_proxy_refusal_is_reported_as_an_egress_block(self):
        # A CONNECT-level refusal means the request never left the machine, so
        # it must not be retried or mistaken for a feed outage.
        slept = []
        opener = FakeOpener(
            urllib.error.URLError("Tunnel connection failed: 403 Forbidden")
        )
        with self.assertRaises(dk.EgressBlocked) as ctx:
            dk.fetch_day(
                "EURUSD", dt.date(2026, 9, 1), opener=opener, sleep=slept.append
            )
        message = str(ctx.exception)
        self.assertIn("datafeed.dukascopy.com is blocked", message)
        self.assertIn("not a feed or broker problem", message)
        self.assertEqual(opener.calls, 1)  # not retried
        self.assertEqual(slept, [])        # no backoff wasted

    def test_egress_block_is_a_fetch_error(self):
        self.assertTrue(issubclass(dk.EgressBlocked, dk.FetchError))

    def test_ordinary_network_errors_still_retry(self):
        opener = FakeOpener(urllib.error.URLError("timed out"), b"payload")
        got = dk.fetch_day(
            "EURUSD", dt.date(2026, 9, 1), opener=opener, sleep=lambda _: None
        )
        self.assertEqual(got, b"payload")

    def test_client_errors_are_not_retried(self):
        opener = FakeOpener(self._error(403))
        with self.assertRaises(dk.FetchError):
            dk.fetch_day("EURUSD", dt.date(2026, 1, 3), opener=opener)
        self.assertEqual(opener.calls, 1)


if __name__ == "__main__":
    unittest.main()
