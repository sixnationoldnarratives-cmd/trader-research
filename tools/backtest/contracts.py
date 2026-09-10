"""Futures contract specifications and the CFD series each is tested against.

The bar-data tree holds CFDs, not contracts. A CFD price tracks the underlying
closely but carries none of the contract's economics, so a backtest that treats
CFD points as money is wrong by a factor of the multiplier and blind to
execution cost. These specs supply what the price series cannot: point value,
tick size, and the commission and slippage a real fill pays.

``proxy_quality`` records how far the CFD stands in for the contract. It is not
decoration -- the runner prints the caveat, because a crude-oil backtest that
looks profitable on a series with no roll yield is the classic way to lose money
on a strategy that tested well.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Contract:
    symbol: str            # exchange symbol, e.g. "GC"
    name: str
    proxy: str             # bar-data symbol standing in for it
    multiplier: float      # account currency per 1.0 of price movement
    tick: float            # minimum price increment
    currency: str
    proxy_quality: str     # "good", "intraday-only" or "poor"
    caveat: str = ""
    commission: float = 2.50   # per contract per side, retail default
    slippage_ticks: float = 1.0

    @property
    def tick_value(self) -> float:
        return self.multiplier * self.tick

    def round_to_tick(self, price: float) -> float:
        return round(price / self.tick) * self.tick

    def cost_per_contract_traded(self) -> float:
        """One side: commission plus assumed slippage."""
        return self.commission + self.slippage_ticks * self.tick_value


CONTRACTS: dict[str, Contract] = {
    c.symbol: c
    for c in [
        Contract(
            "GC", "COMEX Gold (100 oz)", "XAUUSD", 100.0, 0.10, "USD",
            proxy_quality="good",
            caveat="Gold's basis is interest carry only -- small and stable, so "
                   "spot tracks the contract closely.",
        ),
        Contract(
            "MGC", "COMEX Micro Gold (10 oz)", "XAUUSD", 10.0, 0.10, "USD",
            proxy_quality="good",
            caveat="Gold's basis is interest carry only -- small and stable, so "
                   "spot tracks the contract closely.",
        ),
        Contract(
            "NQ", "CME E-mini Nasdaq-100", "USATECHIDXUSD", 20.0, 0.25, "USD",
            proxy_quality="intraday-only",
            caveat="Index basis carries dividends and rates. Fine intraday; "
                   "overnight holds drift from the contract.",
        ),
        Contract(
            "MNQ", "CME Micro Nasdaq-100", "USATECHIDXUSD", 2.0, 0.25, "USD",
            proxy_quality="intraday-only",
            caveat="Index basis carries dividends and rates. Fine intraday; "
                   "overnight holds drift from the contract.",
        ),
        Contract(
            "FDAX", "Eurex DAX", "DEUIDXEUR", 25.0, 1.0, "EUR",
            proxy_quality="intraday-only",
            caveat="DAX is a total-return index, so no dividend drag in the "
                   "basis. Note the index went from 30 to 40 constituents in "
                   "September 2021 -- a structural break mid-sample.",
        ),
        Contract(
            "FDXM", "Eurex Mini-DAX", "DEUIDXEUR", 5.0, 1.0, "EUR",
            proxy_quality="intraday-only",
            caveat="DAX is a total-return index, so no dividend drag in the "
                   "basis. Note the 30-to-40 constituent change in September "
                   "2021 -- a structural break mid-sample.",
        ),
        Contract(
            "CL", "NYMEX WTI Crude (1000 bbl)", "LIGHTCMDUSD", 1000.0, 0.01, "USD",
            proxy_quality="poor",
            caveat="Roll yield drives crude returns and this series has none. "
                   "Intraday signal research only -- any overnight or "
                   "multi-day result will overstate what the contract paid.",
        ),
        Contract(
            "HG", "COMEX Copper (25000 lb)", "COPPERCMDUSD", 25000.0, 0.0005, "USD",
            proxy_quality="poor",
            caveat="Copper carries term structure the CFD cannot represent. "
                   "Intraday only.",
        ),
        Contract(
            "BTC", "CME Bitcoin (5 BTC)", "BTCUSD", 5.0, 5.0, "USD",
            proxy_quality="poor",
            caveat="CME Bitcoin carries a wide, moving basis to spot and stops "
                   "for the weekend while spot does not. A spot-BTC backtest "
                   "will not resemble trading this contract -- use real "
                   "contract data.",
        ),
        Contract(
            "MBT", "CME Micro Bitcoin (0.1 BTC)", "BTCUSD", 0.1, 5.0, "USD",
            proxy_quality="poor",
            caveat="CME Bitcoin carries a wide, moving basis to spot and stops "
                   "for the weekend while spot does not. Use real contract data.",
        ),
    ]
}


def get(symbol: str) -> Contract:
    try:
        return CONTRACTS[symbol.upper()]
    except KeyError:
        known = ", ".join(sorted(CONTRACTS))
        raise KeyError(f"unknown contract {symbol!r}; known: {known}") from None
