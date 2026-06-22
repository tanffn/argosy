"""Deterministic how-to + definition-of-done guidance for plan action items.

The /proposals "What's on you to do" checklist surfaces short/medium-horizon
plan actions as dated to-dos. Each to-do needs three things the raw action
JSON usually lacks:

  * ``how_to``   — concrete steps the user can follow, pointing at the right
    Argosy surface where one exists (``/proposals``, ``/retirement``, etc).
  * ``done_when`` — a crisp, checkable completion criterion ("definition of
    done") so the user knows when to mark it complete.

Priority used by the caller (see ``_collect_action_items``):

  (a) If the underlying horizon-action JSON already carries ``how_to`` /
      ``acceptance`` (done_when) fields (newer synthesizer output), use those.
  (b) ELSE derive ``how_to`` / ``done_when`` here, deterministically, keyed on
      the action's verb/keywords + ``horizon_kind``.

Design rules:
  * NEVER fabricate specific numbers. The guidance gives actionable steps and a
    clear completion bar; concrete figures stay in the action label/detail that
    the plan itself authored.
  * Be genuinely useful, not filler. Each category returns steps a user could
    actually follow today.
  * Fully deterministic + side-effect free, so it is available NOW without any
    re-synthesis and is trivially unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Guidance", "guidance_for_action", "GUIDANCE_CATEGORIES"]


@dataclass(frozen=True)
class Guidance:
    """A (how_to, done_when) pair plus the category that produced it."""

    how_to: str
    done_when: str
    category: str


# Ordered category list (for documentation / tests). The matcher below walks
# keyword groups in this priority order and returns the first hit.
GUIDANCE_CATEGORIES: tuple[str, ...] = (
    "verify_withholding",
    "verify_contribution",
    "verify_check",
    "rebalance_trim_sell",
    "buy_deploy_allocate",
    "contribute_fund",
    "convert_fx",
    "harvest_tax",
    "review_reassess",
    "generic",
)


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(n in text for n in needles)


def guidance_for_action(
    *,
    label: str,
    detail: str = "",
    horizon_kind: str | None = None,
) -> Guidance:
    """Return deterministic (how_to, done_when) guidance for an action.

    Matching is keyword-based over the lower-cased ``label`` (and ``detail``
    as a fallback). The first matching category wins; a useful generic default
    is returned when nothing matches.
    """
    hay = f"{label} {detail}".lower()
    label_l = label.lower()

    # --- verify / check family -------------------------------------------
    # RSU / payroll / tax withholding is the headline example the user named.
    # Match the LABEL (the action's primary subject), NOT label+detail — a SELL
    # action whose detail merely mentions "net-of-tax-withholding" must not be
    # mis-routed to withholding-verification guidance.
    if _contains_any(label_l, ("withhold", "withholding", "section 102", "§102", "rsu tax")):
        return Guidance(
            how_to=(
                "Open your latest payslip (or the Schwab/Etrade RSU vesting "
                "confirmation) and read off the tax actually withheld on the "
                "vest. Compare it to Argosy's §102 estimate on /retirement "
                "(RSU reconciliation card). If the payslip withholding is "
                "lower than the §102 estimate, set aside the shortfall in NIS "
                "cash so you are not surprised at filing."
            ),
            done_when=(
                "You have compared the payslip's withheld tax against the §102 "
                "estimate and either confirmed it is adequate or earmarked cash "
                "for the gap."
            ),
            category="verify_withholding",
        )

    if _contains_any(
        hay,
        ("contribution", "pension", "keren", "hishtalmut", "401k", "ira", "gemel"),
    ) and _contains_any(hay, ("verify", "check", "confirm", "ensure", "adequate", "max")):
        return Guidance(
            how_to=(
                "Pull the year-to-date contributions for this account (employer "
                "portal or the provider statement). Compare against the annual "
                "ceiling / the target rate in your plan. If you are under, "
                "raise the payroll deferral or make a top-up before the "
                "deadline."
            ),
            done_when=(
                "Year-to-date contributions are confirmed on track for the "
                "annual target (or a top-up has been scheduled to close the gap)."
            ),
            category="verify_contribution",
        )

    if _contains_any(hay, ("verify", "check", "confirm", "ensure", "reconcile", "validate")):
        return Guidance(
            how_to=(
                "Find the authoritative source for this figure (payslip, broker "
                "statement, or the matching Argosy surface) and read off the "
                "current value. Compare it to what the plan assumed. If they "
                "diverge materially, note the gap and follow up with the "
                "responsible action."
            ),
            done_when=(
                "The figure has been checked against its source and is either "
                "confirmed correct or the discrepancy has been logged for "
                "follow-up."
            ),
            category="verify_check",
        )

    # --- rebalance / trim / sell -----------------------------------------
    if _contains_any(
        hay,
        ("rebalance", "trim", "reduce", "sell", "lighten", "pare", "de-risk", "derisk"),
    ):
        return Guidance(
            how_to=(
                "Open /proposals and review the sell/trim lines the daily agent "
                "has staged for this position. Confirm they match the plan's "
                "target weight, then accept them so the orders route to the "
                "broker. Cross-check the resulting weight against the target on "
                "/portfolio (current-vs-target)."
            ),
            done_when=(
                "The trim/sell orders have been accepted (or executed at the "
                "broker) and the position's weight on /portfolio sits at or "
                "below its plan target."
            ),
            category="rebalance_trim_sell",
        )

    # --- buy / deploy / allocate -----------------------------------------
    if _contains_any(
        hay,
        ("buy", "deploy", "allocate", "invest", "purchase", "add to", "build position",
         "dollar-cost", "dollar cost", "averaging", "dca", "tranche"),
    ):
        return Guidance(
            how_to=(
                "Open /proposals -> Deploy your cash and review the buy lines "
                "the team has proposed for this allocation. Confirm the tickers "
                "and sizes against the plan target, then accept them to route "
                "the orders. Use /portfolio to confirm the cash balance (USD + "
                "NIS) you are drawing down."
            ),
            done_when=(
                "The buy orders have been accepted and the target cash has been "
                "deployed, so unallocated cash on /portfolio is back at its "
                "intended level."
            ),
            category="buy_deploy_allocate",
        )

    # --- contribute / fund -----------------------------------------------
    if _contains_any(hay, ("contribute", "fund", "top up", "top-up", "deposit", "transfer in")):
        return Guidance(
            how_to=(
                "Make the transfer into the named account from your funding "
                "source (bank or broker), then confirm the deposit landed. If "
                "this is a recurring contribution, set or confirm the standing "
                "order so it repeats automatically."
            ),
            done_when=(
                "The contribution has been transferred and shows as received in "
                "the destination account."
            ),
            category="contribute_fund",
        )

    # --- FX conversion ----------------------------------------------------
    if _contains_any(hay, ("convert", "fx", "usd/nis", "shekel", "currency", "exchange rate")):
        return Guidance(
            how_to=(
                "Check the current USD/NIS rate against the plan's conversion "
                "guidance before acting (the plan's currency-discipline theme "
                "says not to panic-convert). If the conversion is still "
                "warranted, execute it through your broker/bank and record the "
                "rate you got."
            ),
            done_when=(
                "The currency conversion has either been executed at an "
                "acceptable rate or consciously deferred per the plan's "
                "currency-discipline guidance."
            ),
            category="convert_fx",
        )

    # --- tax-loss harvest -------------------------------------------------
    if _contains_any(hay, ("harvest", "tax loss", "tax-loss", "loss harvest", "wash sale")):
        return Guidance(
            how_to=(
                "On /proposals review the harvest line for this position, "
                "confirm the loss is still available and the replacement does "
                "not trigger a wash-sale, then accept it to route the sell. "
                "Re-establish the exposure with the sanctioned replacement "
                "instrument if the plan calls for it."
            ),
            done_when=(
                "The loss-harvest sale has been executed before its deadline "
                "and any required replacement exposure is back on."
            ),
            category="harvest_tax",
        )

    # --- review / reassess ------------------------------------------------
    if _contains_any(
        hay,
        ("review", "reassess", "revisit", "re-evaluate", "reevaluate", "monitor", "watch"),
    ):
        return Guidance(
            how_to=(
                "Re-read the plan's thesis for this item on /plan and check the "
                "current state on the relevant surface (/portfolio, "
                "/retirement, or /proposals). Decide whether the thesis still "
                "holds; if it has shifted, route the change through the "
                "appropriate action rather than leaving it implicit."
            ),
            done_when=(
                "The item has been reviewed against current data and either "
                "confirmed unchanged or a follow-up action has been opened."
            ),
            category="review_reassess",
        )

    # --- generic default --------------------------------------------------
    return Guidance(
        how_to=(
            "Open /plan and read this action's rationale and detail to see what "
            "it is asking for, then take the concrete step on the matching "
            "surface (/proposals for trades and cash, /portfolio for "
            "current-vs-target, /retirement for plan tracking). If the action "
            "implies a trade, the daily agent has likely staged it on "
            "/proposals for you to accept."
        ),
        done_when=(
            "The step described in the action's detail has been carried out and "
            "the outcome is visible on the relevant Argosy surface."
        ),
        category="generic",
    )
