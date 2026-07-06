from __future__ import annotations

# Explicit, versioned fund->constituent weight map for the HOUSEHOLD's held
# broad funds + candidate ETFs. NOT a live holdings feed — a small, cited,
# hand-maintained table sufficient for the correlated-exposure cap. Weights are
# fractions of the fund's NAV. Sources: index fact sheets (S&P 500, Russell 1000
# Growth) as of 2026-Q2; update LOOKTHROUGH_VERSION when refreshed.
# v2 (2026-07-05): completeness pass — every plan-menu instrument (v63 current +
# v64 draft), every held position, and every high-potential seed now has an
# explicit entry, so blind reviewers are never forced onto world-knowledge
# guesses (live finding: DPYA was missing and got mis-guessed as a US
# quality-dividend fund).
# v3 (2026-07-06): plan-change candidate pass — every instrument the plan-change
# team adjudicated (R1GR-replacement growth candidates + diversifier-sleeve
# candidates) has a sourced entry. NVDA/US weights from justETF holdings as of
# 2026-05-29 (conservative-high bound where NVDA sits below the fund's top-10
# disclosure cut). R1GR baseline reconfirmed 13.93% → stays 0.14.
LOOKTHROUGH_VERSION = 3

LOOKTHROUGH_MAP: dict[str, dict[str, float]] = {
    # US broad / growth — carry index NVDA weight.
    "CSPX": {"nvda": 0.07, "us": 1.00},   # iShares Core S&P 500 UCITS
    "VOO": {"nvda": 0.07, "us": 1.00},
    "FUSA": {"nvda": 0.06, "us": 1.00},   # Fidelity US Quality Income
    "R1GR": {"nvda": 0.14, "us": 1.00},   # iShares Russell 1000 Growth
    "SCHG": {"nvda": 0.13, "us": 1.00},
    "QQQM": {"nvda": 0.08, "us": 1.00},
    "SPMV": {"nvda": 0.01, "us": 1.00},   # min-vol underweights NVDA
    "SPMO": {"nvda": 0.10, "us": 1.00},
    "CNDX": {"nvda": 0.14, "us": 1.00},   # iShares Nasdaq 100 — HELD, NVDA-heavy
    "VTV": {"nvda": 0.005, "us": 1.00},   # US value — HELD
    "SCHD": {"nvda": 0.005, "us": 1.00},  # US dividend — HELD
    # World funds — partial US, small NVDA.
    "FWRA": {"nvda": 0.04, "us": 0.65},
    "ACWD": {"nvda": 0.04, "us": 0.63},
    "IWDA": {"nvda": 0.05, "us": 0.70},
    "IWDP": {"nvda": 0.00, "us": 0.55},   # developed property — HELD
    "IUHC": {"nvda": 0.00, "us": 0.70},   # S&P healthcare — HELD
    "XZEW": {"nvda": 0.02, "us": 0.60},   # S&P500 equal-weight ESG — HELD
    "EXUS": {"nvda": 0.00, "us": 0.00},   # World ex-US
    "EIMI": {"nvda": 0.00, "us": 0.00},   # EM
    # iShares Developed Markets Property Yield UCITS (Acc) — plan real-assets
    # sleeve; same FTSE EPRA/NAREIT Dev Div+ index as held IWDP. justETF
    # IE00BFM6T921 as of 2026-05-29: US 63.9%; REIT-only index, NVDA 0.
    "DPYA": {"nvda": 0.00, "us": 0.64},
    # Thematic UCITS — high-potential sleeve seeds.
    # VanEck Semiconductor UCITS: justETF IE00BMC38736 as of 2026-05-29:
    # US 77.1%, NVDA 7.23%.
    "SMGB": {"nvda": 0.07, "us": 0.77},
    # WisdomTree AI UCITS: justETF IE00BDVPNG13 as of 2026-05-28: US 66.4%;
    # NVDA below the top-10 cut (<3.4%), Yahoo holdings showed 3.96% — 0.04
    # is the conservative-high of the two.
    "WTAI": {"nvda": 0.04, "us": 0.66},
    # Growth-sleeve replacement candidates (plan-change team, 2026-07-06).
    # Sources: justETF holdings as of 2026-05-29 unless noted; NVDA weights are
    # conservative-high bounds where NVDA sits below the top-10 disclosure cut.
    # iShares Edge MSCI USA Quality Factor (IE00BD1F4L37): NVDA 6.27% (#3).
    "QDVB": {"nvda": 0.063, "us": 1.00},
    # iShares Edge MSCI World Quality Factor (IE00BP3QZ601): NVDA 5.11% (#3),
    # US 66.66%.
    "IWQU": {"nvda": 0.052, "us": 0.67},
    # iShares Edge MSCI World Momentum (IE00BP3QZ825): NVDA not in top 10
    # (<2.06% bound; REGIME-DEPENDENT — can rotate back ~5-6% at a semi-annual
    # rebalance), US 50.05%.
    "IWMO": {"nvda": 0.02, "us": 0.50},
    # Xtrackers MSCI World Momentum 1C (IE00BL25JP72): same index as IWMO.
    "XDEM": {"nvda": 0.02, "us": 0.51},
    # Xtrackers S&P 500 Equal Weight 1C (IE00BLNMYC90): NVDA ~0.2% BY
    # CONSTRUCTION (durable); 0.005 is the conservative-high entry.
    "XDEW": {"nvda": 0.005, "us": 1.00},
    # Invesco EQQQ Nasdaq-100 Acc (IE00BFZXGZ54): NVDA 8.31% (#1).
    "EQQB": {"nvda": 0.083, "us": 1.00},
    # SPDR S&P 400 US Mid Cap (IE00B4YBJ215): NVDA 0 by construction (large-cap
    # excluded).
    "SPY4": {"nvda": 0.00, "us": 1.00},
    # Xtrackers Russell 2000 1C (IE00BJZ2DD79): NVDA 0 by construction.
    "XRS2": {"nvda": 0.00, "us": 1.00},
    # Invesco NASDAQ-100 Equal Weight (IE000L2SA8K5): ~1% NVDA by construction.
    "EWQA": {"nvda": 0.01, "us": 1.00},
    # Diversifier-sleeve candidates (plan-change team, 2026-07-06).
    # iShares MSCI World Small Cap (IE00BF4RFH31): NVDA 0 (large-cap excluded),
    # US 51.5% (justETF 2026-05-29).
    "WSML": {"nvda": 0.00, "us": 0.52},
    # iShares Global Infrastructure (IE00B1FZS467): NVDA 0, US 62.6%
    # (justETF 2026-05-29).
    "INFR": {"nvda": 0.00, "us": 0.63},
    # VanEck Gold Miners UCITS (IE00BQQP9F84): gold-miner equities (Newmont
    # etc.) — miners are ~half US-listed; NVDA 0. Conservative 0.50 US.
    "GDGB": {"nvda": 0.00, "us": 0.50},
    # L&G All Commodities (IE00BF0BCP69): commodity futures — no equity.
    "BCOM": {"nvda": 0.00, "us": 0.00},
    # Israeli-account index trackers — HELD via Leumi (snapshot symbols are the
    # display names, not exchange tickers).
    "MSCI WORLD": {"nvda": 0.05, "us": 0.70},   # MTF MSCI World tracker — same index look-through as IWDA
    "STOXX EUROPE 600": {"nvda": 0.00, "us": 0.00},  # IBI STOXX Europe 600 tracker — Europe-only
    'מחקה ת"א-200': {"nvda": 0.00, "us": 0.00},      # TA-200 tracker — Israel-only
    # Alternatives / cash-like — zero NVDA, zero US-equity.
    "SGLD": {"nvda": 0.00, "us": 0.00},   # gold ETC
    "IGLN": {"nvda": 0.00, "us": 0.00},
    "SGOV": {"nvda": 0.00, "us": 0.00},
    "IB01": {"nvda": 0.00, "us": 0.00},
    "IBTA": {"nvda": 0.00, "us": 0.00},
    # Direct single-name.
    "NVDA": {"nvda": 1.00, "us": 1.00},
    # Direct US single names — HELD and/or plan v64 high-growth sleeve.
    # Single US stock => us=1.0, nvda=0.0 by construction (no look-through).
    "AMD": {"nvda": 0.00, "us": 1.00},    # HELD; high-potential seed
    "AMZN": {"nvda": 0.00, "us": 1.00},   # HELD
    "BMY": {"nvda": 0.00, "us": 1.00},    # HELD
    "BRK/B": {"nvda": 0.00, "us": 1.00},  # HELD (snapshot symbol uses the slash)
    "CRM": {"nvda": 0.00, "us": 1.00},    # HELD
    "CRWD": {"nvda": 0.00, "us": 1.00},   # plan v64 draft high-growth
    "GOOG": {"nvda": 0.00, "us": 1.00},   # HELD
    "IONQ": {"nvda": 0.00, "us": 1.00},   # plan v64 draft high-growth
    "META": {"nvda": 0.00, "us": 1.00},   # HELD
    "NKE": {"nvda": 0.00, "us": 1.00},    # HELD
    "NOW": {"nvda": 0.00, "us": 1.00},    # HELD
    "O": {"nvda": 0.00, "us": 1.00},      # HELD — Realty Income, US REIT
    "OKLO": {"nvda": 0.00, "us": 1.00},   # plan v64 draft high-growth
    "RKLB": {"nvda": 0.00, "us": 1.00},   # plan v64 draft high-growth
    "RKT": {"nvda": 0.00, "us": 1.00},    # HELD — Rocket Companies
    "RXRX": {"nvda": 0.00, "us": 1.00},   # plan v64 draft high-growth
    "SOFI": {"nvda": 0.00, "us": 1.00},   # HELD; high-potential seed
    # Space Exploration Technologies Corp — the SpaceX stock itself (NASDAQ
    # IPO 2026-06-12, ticker SPCX per etf.com/Yahoo); US company => us=1.0.
    "SPCX": {"nvda": 0.00, "us": 1.00},   # HELD
    "TSLA": {"nvda": 0.00, "us": 1.00},   # HELD; high-potential seed
    # Direct non-US single names — plan v64 draft high-growth sleeve.
    "INVZ": {"nvda": 0.00, "us": 0.00},   # Innoviz — Israeli company (US-listed)
    "MELI": {"nvda": 0.00, "us": 0.00},   # MercadoLibre — LatAm economics (US-listed/DE-inc; geographic, not situs, weight)
    "NU": {"nvda": 0.00, "us": 0.00},     # Nubank — Brazil (Cayman holdco, US-listed)
}


def _weight(symbol: str, key: str) -> float:
    return LOOKTHROUGH_MAP.get(symbol.upper(), {}).get(key, 0.0)


def effective_nvda_usd(symbol: str, notional_usd: float) -> float:
    """Dollars of NVDA exposure a buy of ``notional_usd`` in ``symbol`` adds,
    including index look-through. Unknown symbols contribute 0 (caller tracks
    misses)."""
    return round(notional_usd * _weight(symbol, "nvda"), 2)


def effective_us_usd(symbol: str, notional_usd: float) -> float:
    return round(notional_usd * _weight(symbol, "us"), 2)


def has_lookthrough(symbol: str) -> bool:
    """True when we have an explicit look-through entry for the symbol. A miss
    means ``effective_nvda_usd`` returns 0 — safe for a single non-NVDA stock,
    but UNSAFE for an unmodeled NVDA-heavy fund (it would under-restrict the
    cap). The caller must surface misses as 'concentration unverified' rather
    than silently trusting the 0 (codex H2)."""
    return symbol.upper() in LOOKTHROUGH_MAP
