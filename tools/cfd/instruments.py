"""Per-instrument decoding parameters and plausibility ranges.

Dukascopy stores candle prices as integers scaled by ``10 ** digits``.  Getting
``digits`` wrong silently yields a series that looks like data but is off by a
power of ten -- the failure already visible in the COPPERCMDUSD, LIGHTCMDUSD and
USATECHIDXUSD trees.  So every symbol also declares the price band its real
quotes have lived in, and the downloader refuses to write bars outside it.

``digits_confirmed`` marks symbols whose scale has been checked against bars
already in the repo.  For the rest, run ``verify`` once network egress to
datafeed.dukascopy.com is available.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Repo bar volumes are the feed's float volume times this factor, stored as an
#: integer (EURUSD 3.6 -> 3600000).  Confirmed against the FX and XAUUSD trees.
VOLUME_SCALE = 1_000_000


@dataclass(frozen=True)
class Instrument:
    symbol: str
    digits: int
    sane_low: float
    sane_high: float
    digits_confirmed: bool = True

    def scale(self) -> int:
        return 10**self.digits

    def is_sane(self, price: float) -> bool:
        return self.sane_low <= price <= self.sane_high


def _fx(symbol: str, low: float, high: float, digits: int = 5) -> Instrument:
    return Instrument(symbol, digits, low, high)


# Bands are deliberately generous: they exist to catch a wrong power of ten,
# not to police normal price action.
INSTRUMENTS: dict[str, Instrument] = {
    i.symbol: i
    for i in [
        _fx("EURUSD", 0.5, 2.0),
        _fx("GBPUSD", 0.9, 2.2),
        _fx("AUDUSD", 0.4, 1.2),
        _fx("AUDNZD", 0.9, 1.4),
        _fx("EURGBP", 0.5, 1.0),
        _fx("EURAUD", 1.0, 2.2),
        _fx("EURCAD", 1.1, 1.8),
        _fx("GBPAUD", 1.3, 2.8),
        _fx("GBPNZD", 1.5, 3.2),
        _fx("USDCAD", 0.9, 1.7),
        _fx("CADUSD", 0.55, 1.15),
        _fx("CADEUR", 0.5, 0.9),
        # JPY crosses quote to three decimals.
        _fx("USDJPY", 70.0, 200.0, digits=3),
        _fx("GBPJPY", 90.0, 260.0, digits=3),
        _fx("AUDJPY", 55.0, 120.0, digits=3),
        _fx("CADJPY", 60.0, 125.0, digits=3),
        _fx("NZDJPY", 40.0, 110.0, digits=3),
        Instrument("XAUUSD", 3, 250.0, 6000.0),
        # The three below are the trees whose stored values are not real quotes.
        # Ranges are the instruments' true historical bands; WTI printed
        # negative in April 2020, hence the negative floor.
        Instrument("LIGHTCMDUSD", 3, -60.0, 200.0, digits_confirmed=False),
        Instrument("COPPERCMDUSD", 4, 1.0, 8.0, digits_confirmed=False),
        Instrument("USATECHIDXUSD", 2, 1500.0, 40000.0, digits_confirmed=False),
    ]
}

#: Symbols whose stored history is known to be wrong and needs a clean refetch
#: rather than an incremental top-up.  See tools/README.md.
SUSPECT_SYMBOLS = frozenset(
    s for s, i in INSTRUMENTS.items() if not i.digits_confirmed
)


def get(symbol: str) -> Instrument:
    try:
        return INSTRUMENTS[symbol]
    except KeyError:
        raise KeyError(
            f"unknown instrument {symbol!r}; add it to tools/cfd/instruments.py "
            f"with its digits and a plausible price band"
        ) from None


def suggest_digits(observed_low: float, observed_high: float, inst: Instrument) -> int | None:
    """Return the ``digits`` that would move observed prices into the sane band.

    Used only to make the sanity error actionable -- never to auto-correct, since
    silently guessing a scale is how bad data gets written in the first place.
    """
    for candidate in range(0, 9):
        factor = 10.0 ** (candidate - inst.digits)
        if inst.is_sane(observed_low / factor) and inst.is_sane(observed_high / factor):
            return candidate
    return None
