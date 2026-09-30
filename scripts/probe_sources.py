#!/usr/bin/env python3
"""
Phase 1: source verification for the HYPE buyback dashboard.

Hits every candidate data source, saves the raw payload to samples/, and prints a
coverage report answering the questions Phase 1 actually needs answered:

  - Does the source respond at all, without auth?
  - Is it per-transaction or a daily snapshot?
  - How far back does its history really go?
  - Is there enough of it to fit a slippage curve?

Stdlib only. No pip install. Runs identically on a laptop and in GitHub Actions.

Usage:
    python scripts/probe_sources.py                # probe everything
    python scripts/probe_sources.py --only hl      # hl | llama | hypurrscan
    python scripts/probe_sources.py --no-save      # print report, write nothing
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

HL_INFO = "https://api.hyperliquid.xyz/info"
LLAMA = "https://api.llama.fi"
HYPURRSCAN = "https://api.hypurrscan.io"

# Documented at hyperliquid.gitbook.io/hyperliquid-docs/trading/fees:
# "The assistance fund uses the system address 0xfefefefefefefefefefefefefefefefefefefefe.
#  It converts trading fees to HYPE in a fully automated manner as part of the L1 execution.
#  HYPE in the assistance fund is burned, removing the tokens permanently from the
#  circulating and total supply."
ASSISTANCE_FUND = "0xfefefefefefefefefefefefefefefefefefefefe"

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLES = REPO_ROOT / "samples"

MS_DAY = 86_400_000
NOW_MS = int(time.time() * 1000)

USER_AGENT = "hype-buyback-dashboard/phase1-probe (source verification)"


# --------------------------------------------------------------------------------------
# Probe plumbing
# --------------------------------------------------------------------------------------


@dataclass
class Probe:
    """One source hit and everything Phase 1 wants to record about it."""

    name: str
    source: str
    url: str
    method: str = "GET"
    body: dict[str, Any] | None = None

    ok: bool = False
    status: int | None = None
    error: str | None = None
    elapsed_ms: int = 0
    payload: Any = None
    sample_file: str | None = None
    notes: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.notes.append(msg)


def hit(probe: Probe, timeout: int = 45) -> Probe:
    data = None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if probe.body is not None:
        data = json.dumps(probe.body).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(probe.url, data=data, headers=headers, method=probe.method)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            probe.status = resp.status
            probe.payload = json.loads(raw)
            probe.ok = True
    except urllib.error.HTTPError as exc:
        probe.status = exc.code
        probe.error = f"HTTP {exc.code}: {exc.read()[:300].decode(errors='replace')}"
    except json.JSONDecodeError as exc:
        probe.error = f"response was not JSON: {exc}"
    except Exception as exc:  # noqa: BLE001 - a probe should never crash the run
        probe.error = f"{type(exc).__name__}: {exc}"
    probe.elapsed_ms = int((time.time() - started) * 1000)
    return probe


def save(probe: Probe, save_enabled: bool) -> None:
    if not save_enabled or not probe.ok:
        return
    SAMPLES.mkdir(parents=True, exist_ok=True)
    path = SAMPLES / f"{probe.source}__{probe.name}.json"
    envelope = {
        "_probe": {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "url": probe.url,
            "method": probe.method,
            "request_body": probe.body,
            "http_status": probe.status,
            "elapsed_ms": probe.elapsed_ms,
        },
        "payload": truncate_for_sample(probe.payload),
    }
    path.write_text(json.dumps(envelope, indent=2))
    probe.sample_file = str(path.relative_to(REPO_ROOT))


def truncate_for_sample(payload: Any, max_items: int = 200) -> Any:
    """Keep samples readable. A sample payload is documentation, not a dataset."""
    if isinstance(payload, list) and len(payload) > max_items:
        head = payload[: max_items // 2]
        tail = payload[-(max_items // 2) :]
        return head + [f"__truncated__ {len(payload) - max_items} items omitted"] + tail
    if isinstance(payload, dict):
        return {k: truncate_for_sample(v, max_items) for k, v in payload.items()}
    return payload


def ts_to_date(ms: int | float) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------------------
# Hyperliquid info API
# --------------------------------------------------------------------------------------


def probe_hyperliquid(save_enabled: bool) -> list[Probe]:
    probes: list[Probe] = []

    def run(name: str, body: dict[str, Any]) -> Probe:
        p = hit(Probe(name=name, source="hyperliquid", url=HL_INFO, method="POST", body=body))
        save(p, save_enabled)
        probes.append(p)
        return p

    # --- Zone 1 of the dashboard: live perp market context -----------------------------
    p = run("metaAndAssetCtxs", {"type": "metaAndAssetCtxs"})
    if p.ok and isinstance(p.payload, list) and len(p.payload) == 2:
        universe = p.payload[0].get("universe", [])
        ctxs = p.payload[1]
        p.note(f"{len(universe)} perp markets, {len(ctxs)} asset contexts")
        for meta, ctx in zip(universe, ctxs):
            if meta.get("name") == "HYPE":
                p.note(
                    f"HYPE perp: mid={ctx.get('midPx')} "
                    f"dayNtlVlm={ctx.get('dayNtlVlm')} openInterest={ctx.get('openInterest')}"
                )
                break
        total_vlm = sum(float(c.get("dayNtlVlm", 0) or 0) for c in ctxs)
        p.note(f"24h notional volume across all perps: ${total_vlm:,.0f}")

    # --- Spot context: HYPE spot mid and the @107 / PURR style spot pairs ---------------
    p = run("spotMetaAndAssetCtxs", {"type": "spotMetaAndAssetCtxs"})
    if p.ok and isinstance(p.payload, list) and len(p.payload) == 2:
        tokens = p.payload[0].get("tokens", [])
        p.note(f"{len(tokens)} spot tokens")
        for tok in tokens:
            if tok.get("name") == "HYPE":
                p.note(
                    f"HYPE token index={tok.get('index')} "
                    f"tokenId={tok.get('tokenId')} "
                    f"circulating={tok.get('circulatingSupply')} total={tok.get('totalSupply')}"
                )
                break

    # --- THE critical probe -------------------------------------------------------------
    # Everything in Phase 4c depends on whether the assistance fund's buys are visible as
    # fills with an executed price. If they are, realised slippage is measured, not modelled.
    p = run("spotClearinghouseState_AF", {"type": "spotClearinghouseState", "user": ASSISTANCE_FUND})
    if p.ok:
        balances = (p.payload or {}).get("balances", [])
        p.note(f"assistance fund holds {len(balances)} spot balances")
        for bal in balances:
            p.note(f"  {bal.get('coin')}: total={bal.get('total')} hold={bal.get('hold')}")

    p = run("userFills_AF", {"type": "userFills", "user": ASSISTANCE_FUND, "aggregateByTime": True})
    if p.ok and isinstance(p.payload, list):
        summarise_fills(p, p.payload)

    # Walk backwards in 30 day windows to find how far AF fill history actually reaches.
    # Docs: at most 2000 fills per response, and only the 10000 most recent fills are
    # retained. That retention cap, not the API, is the real constraint on history.
    for days_back in (7, 30, 90, 180, 365):
        start = NOW_MS - days_back * MS_DAY
        p = run(
            f"userFillsByTime_AF_{days_back}d",
            {
                "type": "userFillsByTime",
                "user": ASSISTANCE_FUND,
                "startTime": start,
                "endTime": NOW_MS,
                "aggregateByTime": True,
            },
        )
        if p.ok and isinstance(p.payload, list):
            summarise_fills(p, p.payload, window_days=days_back)
            if len(p.payload) == 0:
                p.note("empty window - AF fill retention likely does not reach this far back")

    # --- Historical price for the slippage join ----------------------------------------
    for interval, days_back in (("1h", 30), ("1d", 365)):
        start = NOW_MS - days_back * MS_DAY
        p = run(
            f"candleSnapshot_HYPE_{interval}",
            {
                "type": "candleSnapshot",
                "req": {"coin": "HYPE", "interval": interval, "startTime": start, "endTime": NOW_MS},
            },
        )
        if p.ok and isinstance(p.payload, list) and p.payload:
            first, last = p.payload[0], p.payload[-1]
            p.note(
                f"{len(p.payload)} candles, {ts_to_date(first['t'])} -> {ts_to_date(last['t'])}"
            )
            p.note(f"fields per candle: {sorted(first.keys())}")
            if len(p.payload) >= 5000:
                p.note("hit the documented 5000 candle cap - backfill must paginate by time")

    return probes


def summarise_fills(p: Probe, fills: list[dict], window_days: int | None = None) -> None:
    """Turn a fills array into the Phase 1 facts: coverage, cadence, and usable sample size."""
    label = f"{window_days}d window" if window_days else "most recent"
    p.note(f"{len(fills)} fills ({label})")
    if not fills:
        return

    times = [f["time"] for f in fills if "time" in f]
    if times:
        p.note(f"range: {ts_to_date(min(times))} -> {ts_to_date(max(times))}")
        span_days = (max(times) - min(times)) / MS_DAY
        if span_days > 0:
            p.note(f"cadence: ~{len(fills) / span_days:.1f} fills/day over {span_days:.1f} days")

    p.note(f"fields per fill: {sorted(fills[0].keys())}")

    hype_buys = [
        f
        for f in fills
        if f.get("coin", "").upper().startswith("HYPE") and f.get("side") in ("B", "b")
    ]
    p.note(f"HYPE buy-side fills: {len(hype_buys)}")

    if hype_buys:
        sizes = [float(f["sz"]) for f in hype_buys if f.get("sz")]
        prices = [float(f["px"]) for f in hype_buys if f.get("px")]
        if sizes and prices:
            notional = sum(s * pr for s, pr in zip(sizes, prices))
            p.note(f"HYPE bought: {sum(sizes):,.2f} tokens, ${notional:,.0f} notional")
            p.note(
                f"fill size: min={min(sizes):,.2f} median={sorted(sizes)[len(sizes)//2]:,.2f} "
                f"max={max(sizes):,.2f}"
            )
            p.note(f"volume weighted avg price: ${notional / sum(sizes):,.4f}")
        # Phase 4c needs enough observations with size dispersion to fit slippage vs size.
        if len(hype_buys) >= 200:
            p.note("VERDICT: sample size supports fitting a slippage curve")
        else:
            p.note(f"VERDICT: only {len(hype_buys)} obs - risk #2 in the plan is live")


# --------------------------------------------------------------------------------------
# DefiLlama
# --------------------------------------------------------------------------------------


def probe_defillama(save_enabled: bool) -> list[Probe]:
    probes: list[Probe] = []

    def run(name: str, path: str) -> Probe:
        p = hit(Probe(name=name, source="defillama", url=f"{LLAMA}{path}"))
        save(p, save_enabled)
        probes.append(p)
        return p

    # Slug discovery first. Guessing the slug is how you end up modelling the wrong protocol.
    p = run("fees_overview", "/overview/fees?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true")
    if p.ok:
        protos = (p.payload or {}).get("protocols", [])
        p.note(f"{len(protos)} protocols in the fees overview")
        matches = [
            x
            for x in protos
            if "hyperliquid" in json.dumps(
                {k: x.get(k) for k in ("name", "slug", "module", "parentProtocol")}
            ).lower()
        ]
        p.note(f"{len(matches)} entries matching 'hyperliquid':")
        for m in matches:
            p.note(
                f"  slug={m.get('slug')!r} name={m.get('name')!r} "
                f"category={m.get('category')} 24h=${_num(m.get('total24h')):,.0f} "
                f"30d=${_num(m.get('total30d')):,.0f} allTime=${_num(m.get('totalAllTime')):,.0f}"
            )

    for slug in ("hyperliquid", "hyperliquid-perp", "hyperliquid-spot"):
        for data_type in ("dailyFees", "dailyRevenue", "dailyHoldersRevenue"):
            p = run(f"summary_{slug}_{data_type}", f"/summary/fees/{slug}?dataType={data_type}")
            if p.ok:
                chart = (p.payload or {}).get("totalDataChart") or []
                p.note(f"displayName={(p.payload or {}).get('displayName')!r}")
                p.note(f"{len(chart)} daily points")
                if chart:
                    p.note(f"range: {ts_to_date(chart[0][0] * 1000)} -> {ts_to_date(chart[-1][0] * 1000)}")
                    p.note(f"latest value: ${_num(chart[-1][1]):,.0f}")
                    p.note(f"total24h=${_num((p.payload or {}).get('total24h')):,.0f}")
                    p.note(f"totalAllTime=${_num((p.payload or {}).get('totalAllTime')):,.0f}")
                p.note(f"methodology: {str((p.payload or {}).get('methodology'))[:300]}")

    return probes


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------------------
# Hypurrscan
# --------------------------------------------------------------------------------------


def probe_hypurrscan(save_enabled: bool) -> list[Probe]:
    probes: list[Probe] = []

    def run(name: str, path: str) -> Probe:
        p = hit(Probe(name=name, source="hypurrscan", url=f"{HYPURRSCAN}{path}"))
        save(p, save_enabled)
        probes.append(p)
        return p

    p = run("tokenDetails_HYPE", "/tokenDetails/HYPE")
    if p.ok:
        d = p.payload or {}
        keys = sorted(d.keys()) if isinstance(d, dict) else "not a dict"
        p.note(f"fields: {keys}")

    p = run("tags_assistance_fund", f"/tags/{ASSISTANCE_FUND}")
    if p.ok:
        p.note(f"tags for the AF address: {json.dumps(p.payload)[:300]}")

    # holdersAtTimeWithLimit is the cheap way to read AF balance historically: the AF is a
    # top holder, so a small limit is enough. Walk back to find where history stops.
    for days_back in (0, 30, 90, 180, 365, 540, 730):
        ts = int((NOW_MS - days_back * MS_DAY) / 1000)
        p = run(f"holdersAtTime_HYPE_{days_back}d", f"/holdersAtTimeWithLimit/HYPE/{ts}/50")
        if p.ok:
            found = extract_af_balance(p.payload)
            if found is not None:
                p.note(f"AF balance at {ts_to_date(ts * 1000)}: {found:,.2f} HYPE")
            else:
                p.note(f"AF not in top 50 holders at {ts_to_date(ts * 1000)} (or no data this far back)")

    # Per-transaction ledger check: does /transfers expose AF inflows at tx granularity?
    frm = int((NOW_MS - 3 * MS_DAY) / 1000)
    to = int(NOW_MS / 1000)
    p = run("transfers_recent_3d", f"/transfers/{frm}/{to}")
    if p.ok and isinstance(p.payload, list):
        p.note(f"{len(p.payload)} transfers in the last 3 days")
        if p.payload:
            p.note(f"fields per transfer: {sorted(p.payload[0].keys())}")
            af_hits = [
                t for t in p.payload if ASSISTANCE_FUND in json.dumps(t).lower()
            ]
            p.note(f"transfers touching the AF address: {len(af_hits)}")

    return probes


def extract_af_balance(payload: Any) -> float | None:
    """Hypurrscan's holder shapes vary. Find the AF row whatever the shape."""
    rows: list = []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("holders", "data", "result"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break

    for row in rows:
        if isinstance(row, dict):
            addr = str(row.get("address") or row.get("user") or row.get("holder") or "").lower()
            if addr == ASSISTANCE_FUND:
                for bal_key in ("balance", "value", "amount", "total"):
                    if bal_key in row:
                        return _num(row[bal_key])
        elif isinstance(row, list) and len(row) >= 2:
            if str(row[0]).lower() == ASSISTANCE_FUND:
                return _num(row[1])
    return None


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------


def print_report(probes: list[Probe]) -> None:
    ok = [p for p in probes if p.ok]
    failed = [p for p in probes if not p.ok]

    print()
    print("=" * 88)
    print(f"PHASE 1 SOURCE VERIFICATION  |  {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"{len(ok)}/{len(probes)} probes succeeded")
    print("=" * 88)

    current_source = None
    for p in probes:
        if p.source != current_source:
            current_source = p.source
            print(f"\n--- {current_source.upper()} " + "-" * (84 - len(current_source)))
        mark = "OK " if p.ok else "FAIL"
        print(f"\n[{mark}] {p.name}  ({p.elapsed_ms}ms)")
        if p.error:
            print(f"       error: {p.error}")
        for note in p.notes:
            print(f"       {note}")
        if p.sample_file:
            print(f"       saved: {p.sample_file}")

    if failed:
        print("\n" + "=" * 88)
        print("FAILURES TO RESOLVE BEFORE PHASE 3")
        for p in failed:
            print(f"  {p.source}/{p.name}: {p.error}")

    print("\n" + "=" * 88)
    print("Sample payloads are in samples/. Fill in the source inventory table in README.md")
    print("from the notes above, then Phase 1 is done.")
    print("=" * 88)


def write_manifest(probes: list[Probe]) -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "assistance_fund_address": ASSISTANCE_FUND,
        "probes": [
            {
                "source": p.source,
                "name": p.name,
                "url": p.url,
                "method": p.method,
                "request_body": p.body,
                "ok": p.ok,
                "http_status": p.status,
                "error": p.error,
                "elapsed_ms": p.elapsed_ms,
                "sample_file": p.sample_file,
                "findings": p.notes,
            }
            for p in probes
        ],
    }
    (SAMPLES / "_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nWrote {SAMPLES / '_manifest.json'}")


# --------------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=["hl", "llama", "hypurrscan"], help="probe one source")
    parser.add_argument("--no-save", action="store_true", help="do not write sample files")
    args = parser.parse_args()

    save_enabled = not args.no_save
    probes: list[Probe] = []

    if args.only in (None, "hl"):
        probes += probe_hyperliquid(save_enabled)
    if args.only in (None, "llama"):
        probes += probe_defillama(save_enabled)
    if args.only in (None, "hypurrscan"):
        probes += probe_hypurrscan(save_enabled)

    print_report(probes)
    if save_enabled:
        write_manifest(probes)

    # Non-zero only if a source is completely dead, so CI flags a real outage but tolerates
    # a single experimental Hypurrscan route going quiet.
    dead_sources = {
        src for src in {p.source for p in probes} if not any(p.ok for p in probes if p.source == src)
    }
    if dead_sources:
        print(f"\nSOURCE COMPLETELY UNREACHABLE: {', '.join(sorted(dead_sources))}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
