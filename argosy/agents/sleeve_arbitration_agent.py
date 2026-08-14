"""Sleeve-level arbitration agent — rules on a SET of redundant fund verdicts.

Gap this closes: per-instrument fund verdicts are individually coherent but
collectively incoherent when every fund in a sleeve is told to TRIM "because
of the others."  No single-instrument agent can reach the correct ruling
("keep the best vehicle, exit the rest") because each analyses one instrument
in isolation.

This agent receives the FULL CLUSTER — all instruments in the same sleeve
(same asset_class/sector/region) that carry a redundancy-flavoured verdict —
and produces a single consolidated ruling:

  - ONE instrument to KEEP (inherits the full sleeve allocation).
  - The OTHERS to SELL or TRIM (redundancy eliminated).
  - An explicit conservation assertion: this is a CONSOLIDATION, not a
    de-risking — the sleeve's total target exposure is preserved.

Judgment dimensions the agent weighs:
  1. DOMICILE / ESTATE SAFETY — are all vehicles estate-safe (UCITS/IE)?
     If any is US-domiciled, that's a strong signal to prefer the UCITS one.
  2. TER / COST — cheapest vehicle wins, IF TER is available.  If TER is
     not in the packet the agent MUST note it as a data_gap and reduce
     conviction; it MUST NOT fabricate an expense ratio.
  3. NVDA LOOK-THROUGH — does any vehicle embed materially more NVDA than
     the others?  Given the household's 58% single-name concentration,
     lower look-through is preferred among otherwise-equal vehicles.
  4. POSITION SIZE — larger positions are cheaper to keep (lower trading
     friction and market-impact); smaller positions are cheaper to exit.
  5. TAX COST OF EXITING — if any position has a large embedded gain, the
     agent should note the exit cost (even without precise lot data).
  6. INDEX FIT — which vehicle best implements the sleeve mandate?

The agent MUST NOT make the keep/exit choice deterministically.  "Keep the
cheapest" is not a rule here — the agent weighs all six dimensions and
produces a reasoned ruling.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from argosy.agents.base import BaseAgent, ConfidenceBand


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

class InstrumentDisposition(BaseModel):
    """Arbitration ruling for one instrument in the sleeve cluster."""

    ticker: str = Field(description="The instrument ticker (upper-cased).")
    # NOTE: The model sometimes emits "disposition" instead of "action".
    # A model_validator below normalises "disposition" → "action" before Pydantic
    # validates the fields, so both forms are accepted.
    action: str = Field(
        description=(
            "KEEP | SELL | TRIM.  Exactly one instrument must carry KEEP. "
            "SELL = exit the entire position; TRIM = reduce but do not exit "
            "(e.g. glide toward exit while managing tax lot timing)."
        )
    )
    conviction: ConfidenceBand = ConfidenceBand.LOW
    rationale: str = Field(
        default="",
        description=(
            "One sentence explaining why this instrument was selected to KEEP, "
            "SELL, or TRIM relative to the others in the cluster.  Must reference "
            "at least one concrete dimension (domicile, size, cost, look-through, "
            "tax cost)."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _normalise_field_names(cls, values: Any) -> Any:
        """Normalise model-emitted field aliases before Pydantic validation.

        The LLM sometimes emits "disposition" instead of "action" (the field
        uses a more natural English synonym for the slot name).  Rather than
        renaming the field (which would break test code and the orchestrator),
        we normalise at the model_validator boundary.
        """
        if not isinstance(values, dict):
            return values
        if "action" not in values and "disposition" in values:
            values = dict(values)
            values["action"] = values.pop("disposition")
        return values


class SleeveArbitrationReport(BaseModel):
    """Consolidated ruling from the sleeve-level arbitration agent."""

    sleeve_key: str = Field(
        default="",
        description=(
            "The sleeve identifier that defines this cluster, e.g. "
            "'Equity/Broad Index/Global'.  Stamped by the orchestrator; "
            "the model need not echo it."
        ),
    )
    keep_ticker: str = Field(
        description=(
            "The ONE instrument selected as the consolidation vehicle. "
            "Must also appear in dispositions with action=KEEP."
        )
    )
    dispositions: list[InstrumentDisposition] = Field(
        description=(
            "One InstrumentDisposition per instrument in the cluster. "
            "Exactly one must have action=KEEP; the rest SELL or TRIM."
        )
    )
    conservation_assertion: str = Field(
        description=(
            "Explicit statement that this ruling is a CONSOLIDATION, not a "
            "de-risking: the sleeve's total target exposure is preserved in "
            "the kept vehicle.  Required; must use the phrase 'total sleeve "
            "exposure is preserved' or equivalent."
        )
    )
    reasoning_md: str = Field(
        default="",
        description=(
            "2-4 sentence advisor-voice explanation of the ruling.  Must "
            "name the keep ticker and the primary reason it was chosen over "
            "the others.  Must not fabricate TER or tracking-error numbers "
            "not in the input."
        ),
    )
    falsifiers: list[str] = Field(
        default_factory=list,
        description=(
            "Concrete falsifiers for the keep decision.  Example: 'A lower-cost "
            "UCITS MSCI All-World vehicle with TER < X bps enters the market.'"
        ),
    )

    @field_validator("falsifiers", mode="before")
    @classmethod
    def _flatten_falsifier_objects(cls, v: Any) -> Any:
        """Normalize falsifiers that the model emits as dicts instead of strings.

        The model sometimes returns falsifiers as objects like
        ``{"claim": "...", "condition": "..."}`` instead of plain strings.
        Extract a text representation in that case so we don't fail validation.
        """
        if not isinstance(v, list):
            return v
        out: list[str] = []
        for item in v:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                # Try common keys the model uses for the claim text.
                for key in ("claim", "text", "statement", "description", "falsifier"):
                    if key in item and isinstance(item[key], str):
                        out.append(item[key])
                        break
                else:
                    # Fallback: join all string values.
                    text = " ".join(str(val) for val in item.values() if isinstance(val, str))
                    if text:
                        out.append(text)
            else:
                out.append(str(item))
        return out

    revisit_triggers: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Typed trigger objects.  Minimum: one dated_event (annual review)."
        ),
    )
    data_gaps: list[str] = Field(
        default_factory=list,
        description=(
            "Dimensions the agent lacked data on (e.g. 'TER not supplied for "
            "any cluster member — cost comparison unavailable')."
        ),
    )
    confidence: ConfidenceBand = ConfidenceBand.LOW
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Source ids cited (sleeve_context/<sleeve_key>, domain_knowledge/tax).",
    )


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class SleeveArbitrationAgent(BaseAgent[SleeveArbitrationReport]):
    """Opus-class sleeve-level arbitration agent.

    Receives the full cluster context for one sleeve (all instruments with
    redundancy-flavoured verdicts) and produces a single consolidated ruling:
    one instrument to KEEP, the rest to SELL or TRIM.

    Judgment belongs to this agent — not to the caller.  The caller assembles
    the deterministic context; this agent interprets it.
    """

    agent_role = "sleeve_arbitration"
    output_model = SleeveArbitrationReport
    require_citations = True

    def build_prompt(
        self,
        *,
        sleeve_key: str,
        instruments: list[dict[str, Any]],
        domain_knowledge: str = "",
    ) -> tuple[str, str, list[tuple[str, str]]]:
        """Build the sleeve-arbitration prompt.

        Args:
            sleeve_key: e.g. "Equity/Broad Index/Global".
            instruments: list of per-instrument dicts, each containing:
                - ticker: str
                - asset_class, sector, region: from instrument_reference
                - estate_safe: bool
                - domicile_country: str | None
                - position_usd_value: float | None
                - position_weight_pct: float | None
                - prior_verdict: str (the standing TRIM/SELL that triggered arbitration)
                - prior_verdict_reasoning: str (the per-instrument agent's rationale)
                - prior_verdict_id: int
                - us_weight: float | None
                - overlap_instruments: list[str]
                - ter_bps: float | None  (almost always None — not in any adapter)
            domain_knowledge: estate/withholding domain_knowledge excerpts.

        Returns:
            (system, user, sources) tuple.
        """
        # --- Context block for the agent ---
        inst_blocks: list[str] = []
        for inst in instruments:
            tk = (inst.get("ticker") or "").upper()
            lines = [f"  [{tk}]"]
            for key, label in [
                ("asset_class", "Asset class"),
                ("sector", "Sector/Style"),
                ("region", "Region"),
                ("estate_safe", "Estate-safe (not US-situs)"),
                ("domicile_country", "Domicile country"),
                ("us_weight", "US-equity weight (fraction)"),
                ("ter_bps", "TER (basis points)"),
                ("position_usd_value", "Current USD value"),
                ("position_weight_pct", "Current portfolio weight %"),
                ("prior_verdict", "Per-instrument standing verdict"),
                ("prior_verdict_reasoning", "Per-instrument rationale"),
            ]:
                val = inst.get(key)
                if val is not None:
                    lines.append(f"    {label}: {val}")
                else:
                    lines.append(f"    {label}: NOT SUPPLIED")
            overlap = inst.get("overlap_instruments") or []
            if overlap:
                lines.append(f"    Overlap instruments flagged: {', '.join(overlap)}")
            inst_blocks.append("\n".join(lines))

        context_block = (
            f"SLEEVE: {sleeve_key}\n"
            f"INSTRUMENTS IN CLUSTER ({len(instruments)}):\n"
            + "\n\n".join(inst_blocks)
        )

        sources: list[tuple[str, str]] = [
            (f"sleeve_context/{sleeve_key.replace('/', '_')}", context_block),
        ]
        if domain_knowledge.strip():
            sources.append(("domain_knowledge/tax", domain_knowledge))

        tickers_str = ", ".join(
            (inst.get("ticker") or "").upper() for inst in instruments
        )

        system = (
            "You are the sleeve-arbitration analyst on the Argosy investment fleet. "
            "Your job is DIFFERENT from the per-instrument fund analyst: you receive a "
            "CLUSTER of redundant funds in the same sleeve and produce ONE consolidated "
            "ruling for the set — not one verdict per fund in isolation.\n\n"
            "The cluster has already been identified deterministically: these instruments "
            "share the same asset class, sector/style, and region, and each carries a "
            "TRIM or SELL verdict from the per-instrument pass.  The problem is that "
            "each per-instrument agent told a fund to trim 'because of the others', so "
            "executing all verdicts naively strips the entire sleeve.\n\n"
            "YOUR RULING MUST:\n"
            "  1. Name exactly ONE instrument to KEEP as the consolidation vehicle.\n"
            "  2. Assign SELL or TRIM to every other instrument in the cluster.\n"
            "  3. State explicitly that this is a CONSOLIDATION, not a de-risking — "
            "the sleeve's total target exposure is preserved in the kept vehicle.\n"
            "  4. Weight these dimensions in your judgment (listed for guidance, not "
            "as a mechanical formula):\n"
            "       a. DOMICILE / ESTATE SAFETY — prefer UCITS (IE) over US-domiciled.\n"
            "       b. COST (TER) — prefer lower cost, but ONLY if TER is in the packet. "
            "If TER is not supplied, add 'TER not supplied for any cluster member' to "
            "data_gaps and do NOT invent numbers.\n"
            "       c. NVDA LOOK-THROUGH — given ~58% household NVDA concentration, "
            "prefer lower embedded NVDA look-through if known.\n"
            "       d. POSITION SIZE — larger positions have lower exit friction.\n"
            "       e. TAX COST — flag any position with a likely embedded gain.\n"
            "       f. INDEX FIT — which vehicle best implements the sleeve mandate.\n\n"
            "RULES:\n"
            "  - NEVER fabricate TER, AUM, or tracking-error numbers not in the packet.\n"
            "  - If a dimension is unknown, add it to data_gaps and lower conviction.\n"
            "  - FALSIFIERS must be CONCRETE (measurable thresholds or specific events), "
            "not degenerate ('if a better fund exists').\n"
            "  - REVISIT TRIGGERS must be typed; minimum one dated_event.\n"
            "  - CONVICTION is for the whole ruling:\n"
            "      HIGH: all key dimensions resolved (domicile, relative size, index fit).\n"
            "      MEDIUM: most dimensions resolved, some data gaps.\n"
            "      LOW: TER and index-fit both unknown, ruling is primarily size-driven.\n"
            "  - This household is ISRAELI-RESIDENT (NRA for US estate-tax). "
            "The $60K US-situs exemption and 40% marginal above it govern domicile choice.\n"
        )

        user = (
            f"Arbitrate the {sleeve_key} sleeve cluster: {tickers_str}.\n\n"
            f"These instruments all carry TRIM/SELL verdicts from the per-instrument "
            f"fund-vehicle pass, each citing overlap with the others.  Executing all "
            f"verdicts naively would exit the entire sleeve.  Your job is to rule on "
            f"the SET and decide which one to keep.\n\n"
            f"Produce a SleeveArbitrationReport with:\n"
            f"  - keep_ticker (the ONE consolidation vehicle)\n"
            f"  - dispositions (one per instrument: KEEP | SELL | TRIM)\n"
            f"  - conservation_assertion (explicit statement sleeve exposure is preserved)\n"
            f"  - reasoning_md (2-4 sentences, advisor voice)\n"
            f"  - falsifiers (≥2 concrete)\n"
            f"  - revisit_triggers (typed; ≥1 dated_event)\n"
            f"  - data_gaps\n"
            f"  - confidence\n"
            f"  - cited_sources\n\n"
            f"Context is in source 'sleeve_context/{sleeve_key.replace('/', '_')}'. "
            f"Domain knowledge is in source 'domain_knowledge/tax'.\n\n"
            f"Do NOT fabricate TER or tracking error.  Missing data → data_gaps + "
            f"lower conviction."
        )

        return (system, user, sources)


__all__ = ["SleeveArbitrationAgent", "SleeveArbitrationReport", "InstrumentDisposition"]
