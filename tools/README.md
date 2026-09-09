# bar-data tooling

Downloads and maintains the Dukascopy bar tree under `bar-data/`. Standard
library only — no install step.

```sh
cd /path/to/trader-research

python3 -m tools.cfd status                    # coverage audit of every symbol
python3 -m tools.cfd download                  # catch every symbol up to yesterday
python3 -m tools.cfd download EURUSD --dry-run # show the plan, fetch nothing
python3 -m tools.cfd download EURUSD --backfill  # also fill interior gaps
python3 -m tools.cfd download LIGHTCMDUSD --from-scratch
python3 -m tools.cfd aggregate EURUSD          # rebuild 8M from 1M
python3 -m tools.cfd merge-repairs             # fold __repair_*.csv into monthly files
python3 -m tools.cfd verify EURUSD --days 3    # refetch stored days and diff
```

## Bringing the data up to date

Copy-paste, on any machine with normal internet access and Python 3.11+.
Nothing to install — the tool is standard library only, and the Dukascopy feed
is free and needs no account, broker or API key:

```sh
git clone -b claude/cfd-data-download-tk2idr <this repo> trader-research
cd trader-research
python3 -m tools.cfd verify EURUSD --days 3   # confirms the decode against committed bars
python3 -m tools.cfd download                 # catches all 21 symbols up
git commit -am "Refresh bar data" && git push
```

`verify` first: if it prints `OK`, the decode settings are right and `download`
can be trusted. If it prints `MISMATCH`, fix `digits` in
`tools/cfd/instruments.py` before writing anything.

## Network access

The feed lives at `datafeed.dukascopy.com`. Behind a filtered network — a Claude
Code web session's egress policy, a corporate proxy — the connection is refused
at CONNECT and the tool stops immediately with:

```
datafeed.dukascopy.com is blocked by this machine's network policy.
The request never reached Dukascopy, so this is not a feed or broker problem
and retrying will not help.
```

The request never leaves the machine, so a working broker account or a resolved
feed outage makes no difference. Either allow the host in the environment's
egress policy — and start a **fresh** session, since a running container does
not pick up a policy change — or run the command somewhere with open internet.

Everything else (`status`, `aggregate`, `merge-repairs`, the tests) works
offline.

## The feed

One file per instrument per UTC day, public and unauthenticated:

```
https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM}/{DD}/BID_candles_min_1.bi5
                                                      ^^ zero-indexed: Jan=00, Dec=11
```

LZMA-compressed ("alone" framing). Decompressed it is a flat array of 24-byte
big-endian records:

```
>I i i i i f   ->  second-offset from day start, open, close, low, high, volume
```

Two things about that layout bite:

- the price order is **open, close, low, high** — not OHLC;
- prices are **signed** integers scaled by the instrument's `digits`, because
  WTI traded below zero in April 2020.

A day with no trading answers 404 or an empty body. Both mean "nothing here",
not failure, and get recorded in `bar-data/_download_state.json` so the day is
never requested again. Saturdays are skipped outright — the market is closed
21:00 Friday to 21:00 Sunday UTC.

## Storage conventions

`bar-data/{1M,8M}/DUKASCOPY_BID/{SYMBOL}/{YYYY-MM}.csv`, header
`openEpochMillis,closeEpochMillis,open,high,low,close,volume,sourceBars`.

- 1M `closeEpochMillis` is `open + 59000`; prices are printed at the
  instrument's precision with trailing zeros stripped (`1.15430` → `1.1543`);
  `volume` is the feed's float volume times 1,000,000, stored as an integer.
- **8M is derived locally, never downloaded.** Bars are bucketed onto an
  absolute 480,000 ms grid and a bucket is emitted only when all eight minutes
  are present, so `sourceBars` is always 8 and `closeEpochMillis` is
  `open + 479000`. Because 480,000 divides a day evenly, buckets never straddle
  a day or month boundary.

`tools/tests/test_store.py` regenerates five committed 8M months from their 1M
sources and asserts the result is identical, so the rule stays pinned to the
tree. Regenerating all 507 months of AUDUSD and EURAUD produced no diff.

## Decode settings are asserted, not assumed

`tools/cfd/instruments.py` holds each symbol's `digits` and the price band its
real quotes have lived in. A decode landing outside that band aborts the symbol
and writes nothing, naming the `digits` value that would fit:

```
LIGHTCMDUSD 2026-08-25: decoded prices span 43476.9..44649.9 with digits=3,
outside the expected -60..200 -- digits=6 would fit. Nothing written.
```

That guard exists because the failure it catches is already in the tree (below).
`digits` for the FX pairs and XAUUSD is confirmed against committed data;
`COPPERCMDUSD`, `LIGHTCMDUSD` and `USATECHIDXUSD` are marked
`digits_confirmed=False` and should be settled with `verify` once the feed is
reachable.

`verify` re-downloads days that are already stored and diffs them row by row.
It is the check that turns the assumptions above — digits, volume scale, record
field order — into something tested against committed ground truth.

## Known state of the data (audit of 2026-09-08)

Run `python3 -m tools.cfd status` for the live picture.

- **Stale everywhere.** Last bars run from 2026-08-25 (commodities/index) back
  through 2026-08-13 (most FX) to 2025-09-18 (USDCAD).
- **Interior months absent**, not just tail gaps: 7 months each in AUDJPY,
  AUDUSD, EURAUD, EURGBP, EURUSD, GBPJPY, GBPUSD, USDJPY and XAUUSD; 12 in
  EURCAD and USDCAD.
- **Empty and thin month files.** A present `YYYY-MM.csv` is not a covered
  month: AUDUSD 2010-06..2010-10 are header-only and 2010-02..2010-05 hold
  87–228 bars where a full month holds ~30,000. EURAUD 2007-01/02 hold 3 and 5.
- **COPPERCMDUSD, LIGHTCMDUSD and USATECHIDXUSD are not real quotes.** They came
  from a different producer than the rest of the tree — bar duration 59999 ms
  instead of 59000, volume almost always 0, and 14-significant-digit prices that
  cannot be an `int / 10^n` decode. The levels are not the instruments: LIGHT
  sits in a 41k–48k band from 2012 to 2026 (WTI ranged about −$37 to $130), and
  USATECHIDX drifts 78,000 to 103,000, +32%, while the Nasdaq 100 did roughly
  +900%. Refetch these with `--from-scratch` rather than extending them.
- **Six symbols have no 8M tree at all** (CADEUR, CADJPY, CADUSD, EURCAD,
  GBPAUD, USDCAD). `aggregate` will build it if that is wanted; it was left
  alone because the omission looks deliberate.
