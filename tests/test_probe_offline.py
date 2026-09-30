#!/usr/bin/env python3
"""
Offline verification for scripts/probe_sources.py.

Serves realistic fixture payloads from a local HTTP server and runs the real probe
functions against it. This exercises the parsing paths that only ever run when a source
responds successfully, which is exactly the code a sandbox with no egress would otherwise
leave untested.

Run: python tests/test_probe_offline.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import probe_sources as ps  # noqa: E402

NOW = int(time.time() * 1000)
AF = ps.ASSISTANCE_FUND

# --------------------------------------------------------------------------------------
# Fixtures shaped like the real responses
# --------------------------------------------------------------------------------------

FILLS = [
    {
        "coin": "HYPE",
        "px": str(round(38.0 + i * 0.01, 4)),
        "sz": str(round(120.0 + i * 7.5, 2)),
        "side": "B",
        "time": NOW - (500 - i) * 60_000,
        "startPosition": "0.0",
        "dir": "Buy",
        "closedPnl": "0.0",
        "hash": f"0x{i:064x}",
        "oid": 1000 + i,
        "crossed": True,
        "fee": "0.0",
        "tid": 9000 + i,
    }
    for i in range(500)
]

CANDLES = [
    {
        "t": NOW - (200 - i) * 3_600_000,
        "T": NOW - (199 - i) * 3_600_000,
        "s": "HYPE",
        "i": "1h",
        "o": "37.9",
        "c": "38.2",
        "h": "38.4",
        "l": "37.7",
        "v": "150000.0",
        "n": 4200,
    }
    for i in range(200)
]

META_AND_CTXS = [
    {"universe": [{"name": "BTC", "szDecimals": 5}, {"name": "HYPE", "szDecimals": 2}]},
    [
        {"midPx": "64000.0", "dayNtlVlm": "1500000000.0", "openInterest": "12000.0"},
        {"midPx": "38.15", "dayNtlVlm": "220000000.0", "openInterest": "9500000.0"},
    ],
]

SPOT_META_AND_CTXS = [
    {
        "tokens": [
            {"name": "USDC", "index": 0, "tokenId": "0x6d1e", "circulatingSupply": "0"},
            {
                "name": "HYPE",
                "index": 150,
                "tokenId": "0x0d01dc56dcaaca66ad901c959b4011ec",
                "circulatingSupply": "270000000.0",
                "totalSupply": "999000000.0",
            },
        ]
    },
    [{"midPx": "38.10", "dayNtlVlm": "40000000.0"}],
]

SPOT_CH_STATE = {
    "balances": [
        {"coin": "HYPE", "token": 150, "total": "31500000.0", "hold": "0.0", "entryNtl": "0.0"},
        {"coin": "USDC", "token": 0, "total": "125000.0", "hold": "0.0", "entryNtl": "0.0"},
    ]
}

LLAMA_SUMMARY = {
    "displayName": "Hyperliquid",
    "total24h": 1985313,
    "total30d": 64832670,
    "totalAllTime": 1250000000,
    "methodology": "Fees paid by users on perp and spot trading.",
    "totalDataChart": [[1700000000 + d * 86400, 1_000_000 + d * 900] for d in range(600)],
}

LLAMA_OVERVIEW = {
    "protocols": [
        {
            "name": "Hyperliquid Perps",
            "slug": "hyperliquid-perp",
            "module": "hyperliquid",
            "category": "Derivatives",
            "total24h": 1985313,
            "total30d": 64832670,
            "totalAllTime": 1250000000,
        },
        {
            "name": "Chamber Vaults",
            "slug": "dhedge",
            "module": "dhedge",
            "category": "Indexes",
            "total24h": 2560,
            "total30d": 146037,
            "totalAllTime": 1671625,
        },
    ]
}

# Hypurrscan holder shapes vary by route, so cover both the dict and the pair-array form.
HOLDERS_DICT = [
    {"address": "0xaaa1", "balance": 40000000.0},
    {"address": AF, "balance": 31500000.0},
]
HOLDERS_PAIRS = [["0xaaa1", 40000000.0], [AF, 29000000.0]]

TRANSFERS = [
    {"time": NOW - 3600_000, "user": "0xaaa1", "destination": AF, "token": "HYPE", "amount": "500.0"},
    {"time": NOW - 7200_000, "user": "0xbbb2", "destination": "0xccc3", "token": "USDC", "amount": "10.0"},
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence the server
        pass

    def _send(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n))
        t = req.get("type")
        if t == "metaAndAssetCtxs":
            return self._send(META_AND_CTXS)
        if t == "spotMetaAndAssetCtxs":
            return self._send(SPOT_META_AND_CTXS)
        if t == "spotClearinghouseState":
            return self._send(SPOT_CH_STATE)
        if t == "userFills":
            return self._send(FILLS)
        if t == "userFillsByTime":
            # Mimic the retention cap: nothing older than ~90 days comes back.
            start = req.get("startTime", 0)
            if NOW - start > 90 * ps.MS_DAY:
                return self._send([])
            return self._send([f for f in FILLS if f["time"] >= start])
        if t == "candleSnapshot":
            return self._send(CANDLES)
        return self._send({"error": f"unhandled type {t}"}, 400)

    def do_GET(self):
        p = self.path
        if p.startswith("/overview/fees"):
            return self._send(LLAMA_OVERVIEW)
        if p.startswith("/summary/fees/"):
            return self._send(LLAMA_SUMMARY)
        if p.startswith("/tokenDetails/"):
            return self._send({"name": "HYPE", "totalSupply": "999000000", "deployer": "0xdead"})
        if p.startswith("/tags/"):
            return self._send({"tags": ["Assistance Fund"]})
        if p.startswith("/holdersAtTimeWithLimit/"):
            # Path is /holdersAtTimeWithLimit/{token}/{ts_seconds}/{limit}. Derive the
            # lookback from the timestamp so the fixture can vary the response shape by
            # age: recent = dict rows, mid history = pair arrays, deep history = empty.
            ts_seconds = int(p.split("/")[3])
            days_back = (NOW / 1000 - ts_seconds) / 86400
            if days_back > 600:
                return self._send([])
            if 60 < days_back < 120:
                return self._send(HOLDERS_PAIRS)
            return self._send(HOLDERS_DICT)
        if p.startswith("/transfers/"):
            return self._send(TRANSFERS)
        return self._send({"error": "not found"}, 404)


def run_checks() -> int:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    base = f"http://127.0.0.1:{port}"
    ps.HL_INFO = f"{base}/info"
    ps.LLAMA = base
    ps.HYPURRSCAN = base

    probes = ps.probe_hyperliquid(False) + ps.probe_defillama(False) + ps.probe_hypurrscan(False)
    server.shutdown()

    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label} {detail}")
            failures.append(label)

    by_name = {p.name: p for p in probes}
    notes = {name: " | ".join(p.notes) for name, p in by_name.items()}

    print("\nParsing checks")
    check("every probe returned 2xx", all(p.ok for p in probes),
          str([p.name for p in probes if not p.ok]))
    check("HYPE perp context extracted", "HYPE perp: mid=38.15" in notes.get("metaAndAssetCtxs", ""))
    check("total perp volume summed", "$1,720,000,000" in notes.get("metaAndAssetCtxs", ""))
    check("HYPE spot token located", "index=150" in notes.get("spotMetaAndAssetCtxs", ""))
    check("AF spot balance read", "HYPE: total=31500000.0" in notes.get("spotClearinghouseState_AF", ""))

    fills_notes = notes.get("userFills_AF", "")
    check("fill count reported", "500 fills" in fills_notes)
    check("HYPE buys isolated", "HYPE buy-side fills: 500" in fills_notes)
    check("notional computed", "notional" in fills_notes)
    check("VWAP computed", "volume weighted avg price" in fills_notes)
    check("sample size verdict fires", "VERDICT: sample size supports" in fills_notes)
    check("cadence computed", "fills/day" in fills_notes)

    check("retention cap detected at 180d",
          "empty window" in notes.get("userFillsByTime_AF_180d", ""))
    check("30d window still returns fills",
          "HYPE buy-side fills:" in notes.get("userFillsByTime_AF_30d", ""))

    candle_notes = notes.get("candleSnapshot_HYPE_1h", "")
    check("candle range reported", "200 candles" in candle_notes)
    check("candle fields listed", "'t'" in candle_notes and "'c'" in candle_notes)

    ov = notes.get("fees_overview", "")
    check("llama slug discovery filters correctly",
          "1 entries matching 'hyperliquid'" in ov and "hyperliquid-perp" in ov,
          ov[:200])
    summ = notes.get("summary_hyperliquid_dailyFees", "")
    check("llama chart range parsed", "600 daily points" in summ)
    check("llama methodology captured", "methodology:" in summ)

    print("\nHypurrscan shape handling")
    check("AF found in dict-shaped holders",
          "AF balance at" in notes.get("holdersAtTime_HYPE_0d", ""))
    check("AF found in pair-array holders",
          "29,000,000.00 HYPE" in notes.get("holdersAtTime_HYPE_90d", ""))
    check("empty history handled without crashing",
          "AF not in top 50" in notes.get("holdersAtTime_HYPE_730d", ""))
    check("AF transfers detected",
          "transfers touching the AF address: 1" in notes.get("transfers_recent_3d", ""))

    print("\nHelper unit checks")
    check("extract_af_balance dict form", ps.extract_af_balance(HOLDERS_DICT) == 31500000.0)
    check("extract_af_balance pair form", ps.extract_af_balance(HOLDERS_PAIRS) == 29000000.0)
    check("extract_af_balance nested", ps.extract_af_balance({"holders": HOLDERS_DICT}) == 31500000.0)
    check("extract_af_balance missing returns None", ps.extract_af_balance([{"address": "0x1"}]) is None)
    check("_num coerces junk to 0", ps._num("abc") == 0.0 and ps._num(None) == 0.0)
    trunc = ps.truncate_for_sample(list(range(1000)), max_items=100)
    check("truncate keeps head and tail", len(trunc) == 101 and trunc[0] == 0 and trunc[-1] == 999)
    check("truncate leaves small lists alone", ps.truncate_for_sample([1, 2, 3]) == [1, 2, 3])

    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {failures}")
        return 1
    print(f"All {len(probes)} probes parsed cleanly. Logic verified offline.")
    return 0


if __name__ == "__main__":
    sys.exit(run_checks())
