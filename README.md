# HYPE Buyback Dashboard

A revenue recognition and cash flow model applied to a protocol. It measures how much HYPE
Hyperliquid's protocol fees remove from supply, treats the Assistance Fund as a
price insensitive bid, and projects implied supply removal and market cap under stated
assumptions.

This is not a price chart. The analytical logic is the product.

**Status:** Phase 1, source verification.

---

## The mechanic being modelled

From the [Hyperliquid fee documentation](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees):

> Fees are entirely directed to the community (HLP, the assistance fund, and deployers).
> The assistance fund uses the system address `0xfefefefefefefefefefefefefefefefefefefefe`.
> It converts trading fees to HYPE in a fully automated manner as part of the L1 execution.
> HYPE in the assistance fund is burned, removing the tokens permanently from the
> circulating and total supply.

Three things follow from that paragraph, and they set the shape of the whole model:

1. **The conversion is automated at the protocol level**, not discretionary. That is what
   makes a fee to buyback conversion ratio a stable enough thing to regress for.
2. **The bid is price insensitive.** It buys because fees arrived, not because the price is
   attractive. That is the assumption the scenario engine rests on.
3. **The removal is a burn, not a treasury holding.** Following a validator vote in
   December 2025, Assistance Fund HYPE is formally out of circulating *and* total supply.
   "Implied supply removed" is therefore literal, not an inference about a fund that might
   one day sell.

## Method

```
protocol fees  ->  x conversion ratio  ->  buy pressure in USD
buy pressure   ->  / avg execution px  ->  tokens acquired
tokens         ->  x slippage curve    ->  realised tokens removed
tokens removed ->  vs float            ->  implied market cap
```

Two of those arrows are estimated rather than observed, and both get their own model with
its fit quality documented:

- **The conversion ratio** is not published. It is inferred by regressing observed buyback
  volume against observed fees.
- **The slippage curve** is fitted from realised fills: `(executed price - prevailing mid) / prevailing mid`
  against trade size, on a rolling 60 day window so it adapts as liquidity deepens.

---

## Source inventory

Run `python scripts/probe_sources.py` to populate this table and drop sample payloads in
`samples/`. Every cell below marked TBD is a Phase 1 deliverable.

| Source | Endpoint | Auth | Cadence | History depth | Grain | Used for |
|---|---|---|---|---|---|---|
| Hyperliquid info | `POST api.hyperliquid.xyz/info` `metaAndAssetCtxs` | None | Real time | Live only | Snapshot | Zone 1 live tiles: mids, volume, OI |
| Hyperliquid info | `POST .../info` `spotMetaAndAssetCtxs` | None | Real time | Live only | Snapshot | HYPE spot mid, circulating and total supply |
| Hyperliquid info | `POST .../info` `spotClearinghouseState` (user = AF) | None | Real time | Live only | Snapshot | Current Assistance Fund HYPE balance |
| Hyperliquid info | `POST .../info` `userFillsByTime` (user = AF) | None | Real time | TBD, capped at 10,000 most recent fills | **Per fill** | Realised buyback execution prices, the slippage model's ground truth |
| Hyperliquid info | `POST .../info` `candleSnapshot` | None | Real time | TBD, 5,000 candles per call | OHLCV bar | Prevailing mid at fill time |
| DefiLlama | `GET api.llama.fi/summary/fees/<slug>` | None | Daily | TBD | Daily | Historical fee and revenue series |
| Hypurrscan | `GET api.hypurrscan.io/holdersAtTimeWithLimit/HYPE/<ts>/<n>` | None | On demand | TBD | Snapshot at timestamp | Assistance Fund balance history, cross check |
| Hypurrscan | `GET api.hypurrscan.io/transfers/<from>/<to>` | None | On demand | TBD | Per transfer | Cross check on AF inflows |
| Dune | Query API | Key, free tier | On demand | Full | Varies | Backup only, if the above is thin |

Hypurrscan publishes an OpenAPI spec at
[api.hypurrscan.io/ui](https://api.hypurrscan.io/ui/). There is no dedicated assistance
fund route: AF balance is read by filtering the holder endpoints to the AF address.

### The question Phase 1 exists to answer

`userFillsByTime` for the Assistance Fund address is the highest value probe in the whole
project. If the AF's buys come back as fills with an executed price and size, then realised
slippage is **measured** rather than modelled, and the plan's risk #2 disappears. If the
retention cap (10,000 most recent fills) only reaches back weeks rather than months, the
historical window narrows and the README says so openly.

The probe script reports exactly this, per lookback window.

---

## Layout

```
scripts/probe_sources.py     Phase 1 source verification harness, stdlib only
samples/                     Raw payloads, one per probe, plus _manifest.json
.github/workflows/           Scheduled probe, and later the ingestion cron
```

## Running the probe

No dependencies. Any Python 3.10 or newer:

```bash
python scripts/probe_sources.py              # everything
python scripts/probe_sources.py --only hl    # just the Hyperliquid endpoints
python scripts/probe_sources.py --no-save    # report only, write nothing
```

It exits non-zero only when a source is completely unreachable, so it is safe to wire
straight into CI as a freshness check.

## Deliberately out of scope

- **Databricks.** This dataset is megabytes. Spark here would be resume padding.
- **Price prediction.** The slippage regression is the modelling content. Nothing forecasts price.
- **Trading features.** This stays a revenue model.
