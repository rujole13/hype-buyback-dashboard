"""
HYPE Buyback Model — dashboard shell.

Phase 5 fills in Zones 2 and 3. What exists today is Zone 1 plus the deployment path, so
that russelllee.net/hype is proved end to end before any modelling depends on it.

Zone 1 reads the Hyperliquid API directly. That is deliberate and survives into the final
build: it is keyless, fast, and genuinely live. Zones 2 and 3 will read a marts export
committed by CI, never BigQuery at request time.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone

import streamlit as st

HL_INFO = "https://api.hyperliquid.xyz/info"
ASSISTANCE_FUND = "0xfefefefefefefefefefefefefefefefefefefefe"
HYPE_TOKEN_ID = "0x0d01dc56dcaaca66ad901c959b4011ec"

st.set_page_config(
    page_title="HYPE Buyback Model",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="collapsed",
)


def hl(body: dict, timeout: int = 15):
    req = urllib.request.Request(
        HL_INFO,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "russelllee.net/hype"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


@st.cache_data(ttl=30, show_spinner=False)
def live_snapshot() -> dict:
    """Zone 1. Cached for 30s so a slider drag never triggers a network call."""
    out: dict = {"errors": []}

    try:
        meta, ctxs = hl({"type": "metaAndAssetCtxs"})
        out["perp_24h_volume"] = sum(float(c.get("dayNtlVlm") or 0) for c in ctxs)
        for m, c in zip(meta.get("universe", []), ctxs):
            if m.get("name") == "HYPE":
                out["hype_perp_mid"] = float(c.get("midPx") or 0)
                out["hype_open_interest"] = float(c.get("openInterest") or 0)
                break
    except Exception as exc:
        out["errors"].append(f"perp context: {exc}")

    try:
        details = hl({"type": "tokenDetails", "tokenId": HYPE_TOKEN_ID})
        out["circulating_supply"] = float(details.get("circulatingSupply") or 0)
        out["total_supply"] = float(details.get("totalSupply") or 0)
    except Exception as exc:
        out["errors"].append(f"token details: {exc}")

    try:
        state = hl({"type": "spotClearinghouseState", "user": ASSISTANCE_FUND})
        for bal in state.get("balances", []):
            if bal.get("coin", "").upper().startswith("HYPE"):
                out["af_balance"] = float(bal.get("total") or 0)
                break
    except Exception as exc:
        out["errors"].append(f"assistance fund: {exc}")

    out["fetched_at"] = datetime.now(timezone.utc)
    return out


def fmt_usd(v: float | None) -> str:
    if not v:
        return "—"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"${v / div:,.2f}{unit}"
    return f"${v:,.2f}"


def fmt_num(v: float | None, suffix: str = "") -> str:
    if not v:
        return "—"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"{v / div:,.2f}{unit}{suffix}"
    return f"{v:,.2f}{suffix}"


# ======================================================================================

st.title("HYPE Buyback Model")
st.caption(
    "Hyperliquid routes trading fees into an automated, price insensitive bid that buys "
    "HYPE and burns it. This models how much supply that removes, and what it implies for "
    "market cap, from observed fees rather than sentiment."
)

snap = live_snapshot()

# -------------------------------------------------------------------- Zone 1: Live -----
st.subheader("Live")

c1, c2, c3, c4 = st.columns(4)
c1.metric("HYPE mid", f"${snap.get('hype_perp_mid', 0):,.2f}" if snap.get("hype_perp_mid") else "—")
c2.metric("Perp volume, 24h", fmt_usd(snap.get("perp_24h_volume")))
c3.metric("Assistance Fund", fmt_num(snap.get("af_balance"), " HYPE"))

adjusted_supply = None
implied_mcap = None
if snap.get("circulating_supply") and snap.get("af_balance") is not None:
    adjusted_supply = snap["circulating_supply"] - snap["af_balance"]
    if snap.get("hype_perp_mid"):
        implied_mcap = snap["hype_perp_mid"] * adjusted_supply
c4.metric("Market cap, ex-Assistance Fund", fmt_usd(implied_mcap))

if snap["errors"]:
    with st.expander(f"{len(snap['errors'])} live source(s) unavailable"):
        for err in snap["errors"]:
            st.write(f"- {err}")

if adjusted_supply is not None:
    st.caption(
        f"Market cap excludes Assistance Fund HYPE, which is burned. Hyperliquid reports "
        f"{snap['circulating_supply'] / 1e6:,.2f}M circulating; less "
        f"{snap['af_balance'] / 1e6:,.2f}M in the fund leaves {adjusted_supply / 1e6:,.2f}M."
    )

st.caption(f"Live values cached 30s. Last fetched {snap['fetched_at']:%Y-%m-%d %H:%M:%S} UTC.")

st.divider()

# ------------------------------------------------------------- Zone 2: Historical ------
st.subheader("Historical")
st.info(
    "Phase 4 and 5. Cumulative fees, cumulative buybacks, and implied supply removed, "
    "read from a marts export committed by CI. Observed fact, no interactivity.",
    icon=":material/schedule:",
)

st.divider()

# --------------------------------------------------------- Zone 3: Scenario engine -----
st.subheader("Scenario engine")
st.info(
    "Phase 5. Sliders for fee growth, buyback share of fees, slippage multiplier and time "
    "horizon, each defaulting to its live value with a marker showing where live sits. "
    "Sliders move assumptions only. Observed history stays locked.",
    icon=":material/schedule:",
)

st.divider()
st.caption(
    "This is a model, not financial advice. Methodology and its limitations are documented "
    "in the dbt docs. Source: github.com/rujole13/hype-buyback-dashboard"
)
