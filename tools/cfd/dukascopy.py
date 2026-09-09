"""Client for the public Dukascopy datafeed.

URL layout (no auth required)::

    https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM}/{DD}/BID_candles_min_1.bi5
                                                          ^^ zero-indexed: Jan=00, Dec=11

Each file holds one UTC day of one-minute BID candles, LZMA-compressed with the
legacy "alone" header.  Decompressed it is a flat array of 24-byte big-endian
records::

    >I i i i i f   ->  second-offset from day start, open, close, low, high, volume

Note the field order: open, **close, low, high** -- not OHLC.  Prices are
integers scaled by the instrument's ``digits``; they are signed, because WTI
traded below zero in April 2020.  Volume is a float in millions of the base
unit, which this repo stores as an integer (see ``VOLUME_SCALE``).

A day with no trading (weekend, holiday) answers 404 or returns an empty body.
Both mean "nothing here", not failure.
"""

from __future__ import annotations

import datetime as dt
import lzma
import math
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from tools.cfd.instruments import VOLUME_SCALE, Instrument, suggest_digits

FEED_HOST = "datafeed.dukascopy.com"
FEED_URL = (
    "https://datafeed.dukascopy.com/datafeed/{symbol}/{year:04d}/{month:02d}"
    "/{day:02d}/BID_candles_min_1.bi5"
)
CANDLE = struct.Struct(">Iiiiif")
USER_AGENT = "trader-research bar-data sync (+stdlib urllib)"

MINUTE_MS = 60_000
#: Repo convention: closeEpochMillis is the last whole second of the minute.
BAR_CLOSE_OFFSET_MS = 59_000


class SanityError(ValueError):
    """Decoded prices fall outside the instrument's plausible band."""


class FetchError(RuntimeError):
    """The feed could not be reached after retrying."""


class EgressBlocked(FetchError):
    """A proxy refused the connection before it reached the feed.

    Distinct from an ordinary fetch failure: retrying, waiting, or a working
    broker account change nothing, because the request never leaves the machine.
    """

    def __init__(self, host: str, detail: str):
        super().__init__(
            f"{host} is blocked by this machine's network policy ({detail}).\n"
            f"The request never reached Dukascopy, so this is not a feed or "
            f"broker problem and retrying will not help.\n"
            f"Fix: allow {host} in the environment's egress policy and start a "
            f"fresh session, or run this command on a machine with open "
            f"internet access."
        )
        self.host = host


#: Substrings a CONNECT-level refusal produces, across urllib and proxy wording.
_EGRESS_MARKERS = (
    "tunnel connection failed",
    "proxy connection failed",
    "cannot connect to proxy",
)


@dataclass(frozen=True)
class Bar:
    open_ms: int
    close_ms: int
    open: str
    high: str
    low: str
    close: str
    volume: int
    source_bars: int = 1

    def row(self) -> list[str]:
        return [
            str(self.open_ms),
            str(self.close_ms),
            self.open,
            self.high,
            self.low,
            self.close,
            str(self.volume),
            str(self.source_bars),
        ]


def format_price(value: float, digits: int) -> str:
    """Render a price the way the existing CSVs do: fixed precision, zeros stripped."""
    text = f"{value:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def day_url(symbol: str, day: dt.date) -> str:
    return FEED_URL.format(
        symbol=symbol, year=day.year, month=day.month - 1, day=day.day
    )


def fetch_day(
    symbol: str,
    day: dt.date,
    *,
    retries: int = 4,
    timeout: float = 60.0,
    opener: urllib.request.OpenerDirector | None = None,
    sleep=time.sleep,
) -> bytes | None:
    """Download one day's candle file.  Returns ``None`` when the day has no data."""
    url = day_url(symbol, day)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    get = (opener.open if opener else urllib.request.urlopen)
    delay = 2.0
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with get(request, timeout=timeout) as response:
                return response.read() or None
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            # 4xx other than 404 will not fix themselves; only retry 5xx and 429.
            if exc.code < 500 and exc.code != 429:
                raise FetchError(f"{url} -> HTTP {exc.code}") from exc
            last = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
        if _is_egress_block(last):
            raise EgressBlocked(FEED_HOST, str(last)) from last
        if attempt < retries:
            sleep(delay)
            delay *= 2
    raise FetchError(f"{url} failed after {retries + 1} attempts: {last}")


def _is_egress_block(exc: Exception | None) -> bool:
    return exc is not None and any(m in str(exc).lower() for m in _EGRESS_MARKERS)


def decompress(blob: bytes) -> bytes:
    """Inflate a .bi5 payload, tolerating either LZMA framing."""
    for fmt in (lzma.FORMAT_ALONE, lzma.FORMAT_AUTO):
        try:
            return lzma.LZMADecompressor(format=fmt).decompress(blob)
        except lzma.LZMAError:
            continue
    raise ValueError("payload is not LZMA-compressed")


def decode_day(blob: bytes, day: dt.date, inst: Instrument) -> list[Bar]:
    """Decode one day's candle file into repo-shaped bars.

    Raises :class:`SanityError` if the prices do not land in the instrument's
    plausible band -- almost always a wrong ``digits`` setting.
    """
    raw = decompress(blob)
    if len(raw) % CANDLE.size:
        raise ValueError(
            f"{inst.symbol} {day}: {len(raw)} bytes is not a whole number of "
            f"{CANDLE.size}-byte candles"
        )

    day_start_ms = int(
        dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc).timestamp() * 1000
    )
    scale = float(inst.scale())
    bars: list[Bar] = []
    low_seen = math.inf
    high_seen = -math.inf

    for offset in range(0, len(raw), CANDLE.size):
        secs, o, c, lo, hi, vol = CANDLE.unpack_from(raw, offset)
        if o == c == lo == hi == 0:
            # Padding for a minute the feed never filled.
            continue
        prices = [p / scale for p in (o, hi, lo, c)]
        low_seen = min(low_seen, min(prices))
        high_seen = max(high_seen, max(prices))
        open_ms = day_start_ms + secs * 1000
        bars.append(
            Bar(
                open_ms=open_ms,
                close_ms=open_ms + BAR_CLOSE_OFFSET_MS,
                open=format_price(prices[0], inst.digits),
                high=format_price(prices[1], inst.digits),
                low=format_price(prices[2], inst.digits),
                close=format_price(prices[3], inst.digits),
                volume=0 if math.isnan(vol) else round(vol * VOLUME_SCALE),
            )
        )

    if bars and not (inst.is_sane(low_seen) and inst.is_sane(high_seen)):
        hint = suggest_digits(low_seen, high_seen, inst)
        fix = (
            f"digits={hint} would fit"
            if hint is not None
            else "no power of ten fits; check the symbol and the record layout"
        )
        raise SanityError(
            f"{inst.symbol} {day}: decoded prices span "
            f"{low_seen:.6g}..{high_seen:.6g} with digits={inst.digits}, outside the "
            f"expected {inst.sane_low:g}..{inst.sane_high:g} -- {fix}. Nothing written."
        )
    bars.sort(key=lambda b: b.open_ms)
    return bars


def download_day(symbol: str, day: dt.date, inst: Instrument, **kwargs) -> list[Bar]:
    blob = fetch_day(symbol, day, **kwargs)
    if not blob:
        return []
    return decode_day(blob, day, inst)
