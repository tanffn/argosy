"""§102 equity-tax payslip-reconciliation adequacy check.

This answers a single concrete question from a parsed Hilan payslip: **is the
§102 equity (RSU/ESPP) tax — withheld at sale by the trustee and reconciled
through your payslip — adequate for the equity income recognized this year?**

In Israel the §102 equity tax is withheld *at sale*: the §102 trustee/broker
deducts the advance tax from the sale proceeds (the same §102 tax the
``cash_source_reconciler`` sees as sale → Leumi USD wire *net of* tax). It is
then **reconciled through the monthly payslip** — if too much was withheld at
sale the employee is refunded the difference through payroll; if too little,
additional tax is taken through payroll. The payslip's YTD equity tax figure is
therefore the §102 equity tax *reconciled through payroll* on the equity income
recognized year-to-date.

The ground-truth model — confirmed against the real April 2026 payslip — is
that the payslip's §102 equity tax accounted YTD reconciles to the §102 trustee
model at the *wire* ordinary rate (50% top marginal, not the sim's conservative
62.17%):

    accounted_equity_tax  ==  capital_base * capital_rate(0.25)
                            +  ordinary_base * WIRE_ORDINARY_RATE(0.50)

mapping the parser's YTD fields as:

    ordinary_base  = ytd_non_fixed_gross          (§102 ordinary income base)
    capital_base   = ytd_capital_gain             (§102 capital income base)
    accounted_tax  = ytd_tax_on_non_fixed_gross   (§102 equity tax via payslip)

For April: 549,467*0.25 + 60,679*0.50 = 167,706.25 ≈ 167,707 (accounted). The
§102 equity tax accounted through the payslip therefore *reconciles* to the
§102 model at the 50% ordinary rate.

Adequacy vs reconciliation
--------------------------
Reconciliation only tells us the payslip accounts the §102 equity tax the model
predicts at the wire rate. The *final* filing liability for the ordinary
portion is computed at the conservative top-bracket rate (the sim's 0.6217,
which folds in National Insurance / health on the equity band). The §102 tax is
accounted at ~50% on the ordinary band but the conservative estimate is ~62%,
so the year-end filing can owe a top-up (or, if accounted exceeds it, a refund
— which itself flows back through your paycheck):

    conservative_liability = capital_base * 0.25 + ordinary_base * 0.6217
    potential_filing_topup = max(0, conservative_liability - accounted_tax)

That top-up is the honest "set this aside" number. If it is ~0 or negative,
the §102 tax accounted is adequate (a refund is even possible).

Scope honesty
-------------
This check verifies the §102 equity tax **reconciled through the payslip**
against the model. The at-sale advance itself (sale → Leumi wire, net of §102
tax) is captured by the existing ``cash_source_reconciler``. Both look at the
same §102 tax from different documents — the payslip is the
reconciliation / truth-up of what the trustee withheld at sale. This module
does not re-derive the at-sale advance; it only checks that the §102 equity tax
accounted through the payslip matches the model and flags the filing-time
top-up (or refund).

Read-only, deterministic, tolerance-based. No external deps; no UI/route
wiring (that is the next step).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from argosy.services.payslip_parser import LOW, PayslipFacts
from argosy.services.rsu_reconciliation.sim_tax import WIRE_ORDINARY_RATE

# §102 rate constants — REUSED from sim_tax where one exists; the capital and
# conservative-ordinary rates are the sim's verified constants (see sim_tax
# module docstring, verified to 0.0 USD residual against the sim sheet).
CAPITAL_RATE = 0.25
SIM_ORDINARY_RATE = 0.6217  # conservative top-bracket (incl. NI/health band)
# WIRE_ORDINARY_RATE (0.50) imported from sim_tax — the at-wire / at-payroll
# effective ordinary withholding rate.

# Reconciliation tolerance: the larger of $50 or 0.5% of the actual withheld.
_TOL_ABS = 50.0
_TOL_FRAC = 0.005

# Verdict statuses.
STATUS_RECONCILED = "reconciled"
STATUS_DISCREPANCY = "discrepancy"
STATUS_NO_EQUITY = "no_equity_yet"
STATUS_LOW_CONFIDENCE = "low_confidence"

# The YTD equity fields this check depends on.
_EQUITY_FIELDS = (
    "ytd_non_fixed_gross",
    "ytd_capital_gain",
    "ytd_tax_on_non_fixed_gross",
)


@dataclass
class WithholdingVerdict:
    """Adequacy verdict for the §102 equity tax reconciled through the payslip.

    The §102 equity tax is withheld at sale by the trustee and reconciled
    through the payslip (refunded if over-withheld at sale). This verdict checks
    the tax accounted through the payslip against the model.

    All monetary fields are in the payslip's currency (NIS, ₪). ``None`` for a
    derived number means it could not be computed (e.g. no equity yet); never a
    fabricated default.
    """

    status: str  # one of the STATUS_* constants
    period: int | None  # tax year

    # Bases (from the parser's YTD equity fields).
    equity_ordinary_base: float | None  # = ytd_non_fixed_gross
    equity_capital_base: float | None  # = ytd_capital_gain
    actual_tax_withheld: float | None  # = ytd_tax_on_non_fixed_gross (§102 tax via payslip)

    # §102 reconciliation at the wire (50%) ordinary rate.
    expected_at_wire_rate: float | None  # capital*0.25 + ordinary*0.50
    reconc_residual: float | None  # actual - expected_at_wire (signed)

    # Filing-time adequacy at the conservative (62.17%) ordinary rate.
    conservative_liability: float | None  # capital*0.25 + ordinary*0.6217
    potential_filing_topup: float | None  # max(0, conservative - actual)

    effective_rate_pct: float | None  # actual / (capital + ordinary) * 100

    summary: str
    confidence: str  # "high" | "medium" | "low"
    caveats: list[str] = field(default_factory=list)

    # Currency suffix kept abstract; these are NIS today. Field names omit the
    # _usd suffix deliberately because the payslip is denominated in NIS.


def _fmt(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"₪{x:,.0f}"


def check_withholding(facts: PayslipFacts) -> WithholdingVerdict:
    """Verify §102 equity-tax payslip-reconciliation adequacy from a payslip.

    See the module docstring for the model. Returns a :class:`WithholdingVerdict`
    with a plain-language ``summary`` and an honest ``confidence`` + ``caveats``.
    """
    period = facts.period_year

    # The standing caveat about scope: this check verifies the §102 equity tax
    # reconciled through the payslip; the at-sale advance itself (sale → wire) is
    # captured by cash_source_reconciler. Both look at the same §102 tax from
    # different documents — the payslip is the reconciliation / truth-up.
    scope_caveat = (
        "Verifies the §102 equity tax reconciled through your payslip against"
        " the model. The at-sale advance itself (sale → Leumi wire, net of §102"
        " tax) is captured by the cash-source reconciler — same §102 tax, seen"
        " from the other document; the payslip is the truth-up (refunded through"
        " payroll if over-withheld at sale)."
    )

    ord_base = facts.ytd_non_fixed_gross
    cap_base = facts.ytd_capital_gain
    actual = facts.ytd_tax_on_non_fixed_gross

    # ------------------------------------------------------------------
    # 1a. No equity vested yet — the YTD equity fields are absent.
    # ------------------------------------------------------------------
    if ord_base is None and cap_base is None and actual is None:
        return WithholdingVerdict(
            status=STATUS_NO_EQUITY,
            period=period,
            equity_ordinary_base=None,
            equity_capital_base=None,
            actual_tax_withheld=None,
            expected_at_wire_rate=None,
            reconc_residual=None,
            conservative_liability=None,
            potential_filing_topup=None,
            effective_rate_pct=None,
            summary=(
                "No equity (RSU/ESPP) income has accrued year-to-date, so there"
                " is no §102 equity tax reconciled through the payslip to verify"
                " yet. This check will activate on the first month equity income"
                " is recognized."
            ),
            confidence="high",
            caveats=[scope_caveat],
        )

    # ------------------------------------------------------------------
    # 1b. Partial / inconsistent equity fields — cannot assert a number.
    #     If some equity fields are present but others are missing, the
    #     parse is incomplete; don't compute on a hole.
    # ------------------------------------------------------------------
    present = [v is not None for v in (ord_base, cap_base, actual)]
    if not all(present):
        missing = [
            name
            for name, v in zip(
                _EQUITY_FIELDS, (ord_base, cap_base, actual), strict=True
            )
            if v is None
        ]
        return WithholdingVerdict(
            status=STATUS_LOW_CONFIDENCE,
            period=period,
            equity_ordinary_base=ord_base,
            equity_capital_base=cap_base,
            actual_tax_withheld=actual,
            expected_at_wire_rate=None,
            reconc_residual=None,
            conservative_liability=None,
            potential_filing_topup=None,
            effective_rate_pct=None,
            summary=(
                "Equity income has accrued but the payslip's YTD equity fields"
                f" are incomplete (missing: {', '.join(missing)}); the §102"
                " equity-tax reconciliation cannot be verified for this payslip."
                " Re-check the parse."
            ),
            confidence="low",
            caveats=[
                scope_caveat,
                "One or more YTD equity fields could not be located in this"
                " payslip; no withholding number is asserted.",
            ],
        )

    # ------------------------------------------------------------------
    # 1c. Low parser confidence on any equity field — present but untrusted.
    # ------------------------------------------------------------------
    low_conf_fields = [
        name for name in _EQUITY_FIELDS if facts.confidence.get(name) == LOW
    ]
    if low_conf_fields:
        # Still surface the bases so the user sees what was read, but do not
        # assert reconcile/discrepancy on numbers the parser distrusts.
        return WithholdingVerdict(
            status=STATUS_LOW_CONFIDENCE,
            period=period,
            equity_ordinary_base=ord_base,
            equity_capital_base=cap_base,
            actual_tax_withheld=actual,
            expected_at_wire_rate=None,
            reconc_residual=None,
            conservative_liability=None,
            potential_filing_topup=None,
            effective_rate_pct=None,
            summary=(
                "The payslip's YTD equity fields parsed at low confidence"
                f" ({', '.join(low_conf_fields)}); the §102 equity-tax"
                " reconciliation is not asserted on numbers that could not be"
                " corroborated."
            ),
            confidence="low",
            caveats=[
                scope_caveat,
                "Parser marked one or more equity fields low-confidence; the"
                " §102 reconciliation is withheld to avoid asserting an"
                " unverified number.",
            ],
        )

    # ------------------------------------------------------------------
    # 2. Compute the §102 reconciliation at the wire ordinary rate.
    # ------------------------------------------------------------------
    expected_at_wire = cap_base * CAPITAL_RATE + ord_base * WIRE_ORDINARY_RATE
    residual = actual - expected_at_wire

    # ------------------------------------------------------------------
    # 3. ALWAYS compute the conservative filing liability + top-up.
    # ------------------------------------------------------------------
    conservative = cap_base * CAPITAL_RATE + ord_base * SIM_ORDINARY_RATE
    topup = max(0.0, conservative - actual)

    total_base = cap_base + ord_base
    eff_rate = (actual / total_base * 100.0) if total_base > 0 else None

    # Tolerance: larger of $50 or 0.5% of actual.
    tol = max(_TOL_ABS, _TOL_FRAC * abs(actual))
    reconciled = abs(residual) <= tol

    caveats = [
        scope_caveat,
        "The §102 ordinary equity band is accounted at ~50% (top marginal"
        " 47% + 3% surtax); the conservative filing estimate uses ~62.17%"
        " (adds the National Insurance / health band on equity). The top-up is"
        " the gap, and only bites if your marginal ordinary rate is the top"
        " bracket.",
        "Filing liability can still adjust for NI/health caps, annual surtax,"
        " FX/NIS basis, and credits; this is the §102 equity tax reconciled"
        " through the payslip, not the final return.",
    ]

    if reconciled:
        status = STATUS_RECONCILED
        if topup > tol:
            adequacy = (
                f"The §102 equity tax reconciles, but set aside ~{_fmt(topup)}"
                " for a filing-time top-up if your marginal ordinary rate is the"
                " top bracket (the ordinary band was accounted at ~50%, the"
                " conservative estimate is ~62%)."
            )
        else:
            adequacy = (
                "The §102 equity tax looks adequate — the conservative filing"
                " estimate does not exceed what was already accounted, so a"
                " top-up is unlikely (a refund — paid back through payroll — is"
                " even possible)."
            )
        summary = (
            f"Your payslip reconciles {_fmt(actual)} of §102 equity tax YTD on"
            f" {_fmt(ord_base)} ordinary + {_fmt(cap_base)} capital equity"
            f" income. That tax was withheld at sale by the §102 trustee and"
            f" trued up through payroll, and it matches the §102 model at the"
            f" 50% wire rate (expected {_fmt(expected_at_wire)}, residual"
            f" {_fmt(residual)}). {adequacy}"
        )
        confidence = "high"
    else:
        status = STATUS_DISCREPANCY
        summary = (
            f"Your payslip reconciles {_fmt(actual)} of §102 equity tax YTD, but"
            f" the §102 model at the 50% wire rate expects"
            f" {_fmt(expected_at_wire)} (on {_fmt(ord_base)} ordinary +"
            f" {_fmt(cap_base)} capital). That is a residual of {_fmt(residual)}"
            f" (tolerance {_fmt(tol)}) — the §102 equity tax accounted through"
            " the payslip does not match the §102 model; investigate (a separate"
            f" ~{_fmt(topup)} filing top-up may also apply)."
        )
        confidence = "high"
        caveats.insert(
            1,
            "Reconciliation residual exceeds tolerance: the §102 equity tax"
            " accounted through the payslip diverges from the §102 wire-rate"
            " model; verify the parsed bases and the payslip before relying on"
            " the top-up number.",
        )

    return WithholdingVerdict(
        status=status,
        period=period,
        equity_ordinary_base=round(ord_base, 2),
        equity_capital_base=round(cap_base, 2),
        actual_tax_withheld=round(actual, 2),
        expected_at_wire_rate=round(expected_at_wire, 2),
        reconc_residual=round(residual, 2),
        conservative_liability=round(conservative, 2),
        potential_filing_topup=round(topup, 2),
        effective_rate_pct=round(eff_rate, 2) if eff_rate is not None else None,
        summary=summary,
        confidence=confidence,
        caveats=caveats,
    )


__all__ = [
    "WithholdingVerdict",
    "check_withholding",
    "CAPITAL_RATE",
    "SIM_ORDINARY_RATE",
    "STATUS_RECONCILED",
    "STATUS_DISCREPANCY",
    "STATUS_NO_EQUITY",
    "STATUS_LOW_CONFIDENCE",
]
