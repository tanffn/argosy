"""Vintage gate — period-vs-period provenance check (Stream A).

Option C — rules are class-specific (see ``instrument_class``):

  * ``single_name_equity`` — compare the fiscal period the data *covers*
    (``financials_as_of``) to the most recent fiscal period that has been
    *reported* (``most_recent_reported_period``, from SEC EDGAR). Stale
    means the payload covers an earlier period than the latest reported
    one. Sources on each side of the check must be independent.
  * ``fund_etf_index`` — named exemption ``FUND_NO_ISSUER_FISCAL_QUARTER``
    (a fund has no issuer fiscal quarter; equity-quarter gating is wrong).
  * ``cash_tbill`` — named exemption ``CASH_OR_TBILL_NO_FISCAL_VINTAGE``.
  * ``unknown`` — fail closed; never inherits another class's rule.

Unknown equity provenance (blocker 1): missing data period OR missing
reported period is a BLOCK — never a silent pass. Never invents dates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from argosy.services.decision_integrity.as_of import (
    LOAD_BEARING_FINANCIAL_FIELDS,
    field_as_of,
    parse_date,
)
from argosy.services.decision_integrity.instrument_class import (
    FiscalVintageRule,
    ProvenanceClass,
    annotate_provenance_class,
    classify_from_fields,
)


@dataclass(frozen=True)
class StaleField:
    field: str
    as_of: date
    reported_period: date
    value: Any = None


@dataclass(frozen=True)
class VintageGateResult:
    ok: bool
    ticker: str
    data_period: date | None = None
    most_recent_reported_period: date | None = None
    stale_fields: tuple[StaleField, ...] = ()
    reason: str = ""
    blocked_by: str | None = None
    provenance_class: str | None = None
    fiscal_vintage_rule: str | None = None

    @property
    def block(self) -> bool:
        return not self.ok

    # Back-compat alias used by earlier tests / call sites.
    @property
    def most_recent_earnings_date(self) -> date | None:
        return self.most_recent_reported_period


_EQUITY_DATA_SOURCES: frozenset[str] = frozenset(
    {
        "yfinance.mostRecentQuarter",
        "yfinance.lastFiscalYearEnd",
        "finnhub.series.quarterly",
        "finnhub.metric",
    }
)
_EQUITY_REPORTED_SOURCES: frozenset[str] = frozenset(
    {
        "sec.submissions.reportDate",
        "sec.companyfacts",
    }
)


def _source_independence_violation(payload: dict[str, Any]) -> str | None:
    """Both sides of the period check must not share one provider.

    ``financials_as_of`` may come from yfinance/Finnhub; reported period
    must come from SEC (Option C). If both sides name the same family,
    the lag check is not independent.

    Missing source labels on a payload that carries both dates is also a
    violation — unlabeled provenance must never read as satisfied.
    """
    data_src = str(payload.get("financials_as_of_source") or "").strip()
    reported_src = str(
        payload.get("most_recent_reported_period_source")
        or payload.get("reported_period_source")
        or ""
    ).strip()
    has_data_date = parse_date(payload.get("financials_as_of")) is not None
    has_reported_date = parse_date(
        payload.get("most_recent_reported_period")
    ) is not None
    # Two dates without source labels → not an independent check.
    if has_data_date and has_reported_date and (not data_src or not reported_src):
        return (
            "independence_violation: both period dates present but provenance "
            f"source labels missing (financials_as_of_source={data_src!r}, "
            f"most_recent_reported_period_source={reported_src!r})"
        )
    if not data_src or not reported_src:
        return None
    if data_src == reported_src:
        return (
            f"independence_violation: financials_as_of_source={data_src!r} "
            f"equals most_recent_reported_period_source={reported_src!r}"
        )
    # Same family: both yfinance*, or both finnhub*, or both sec*.
    def _family(s: str) -> str:
        return s.split(".", 1)[0].lower()

    if _family(data_src) == _family(reported_src):
        return (
            f"independence_violation: both sides from {_family(data_src)!r} "
            f"({data_src} vs {reported_src})"
        )
    # Soft guide: equity path expects SEC on the reported side when tagged.
    if (
        data_src in _EQUITY_DATA_SOURCES
        and reported_src
        and reported_src not in _EQUITY_REPORTED_SOURCES
        and not reported_src.startswith("sec.")
    ):
        return (
            f"independence_violation: reported period source {reported_src!r} "
            "is not SEC for equity vintage"
        )
    return None


def _sec_enrichment_outcome(
    payload: dict[str, Any],
    *,
    ticker: str,
    cls: ProvenanceClass,
    rule: FiscalVintageRule,
    data_period: date | None,
) -> VintageGateResult | None:
    """Map enrichment sidecar to a gate result when SEC did not yield a period.

    Outage policy (Stream A): confirmed SEC provider outages (403 / 429 /
    timeout / other HTTP) FAIL OPEN with a loud named exemption
    ``sec_provider_outage``. Failing closed would freeze every equity
    recommendation during an EDGAR outage. The exemption is recoverable
    (next gather retries) and must surface on actionable recommendations.

    Misconfiguration (``ARGOSY_SEC_CONTACT_EMAIL`` unset) FAIL CLOSED —
    that is an operator error, not an issuer data fact.
    """
    enrich = str(payload.get("reported_period_enrichment") or "").strip()
    err = str(payload.get("reported_period_enrichment_error") or "")[:200]
    if enrich == "sec_contact_email_unset":
        return VintageGateResult(
            ok=False,
            ticker=ticker,
            data_period=data_period,
            reason=(
                f"sec_config_error:{ticker}: ARGOSY_SEC_CONTACT_EMAIL unset "
                f"or local — not an issuer data outcome. {err}"
            ).strip(),
            blocked_by="sec_config_error",
            provenance_class=cls.value,
            fiscal_vintage_rule=rule.value,
        )
    if enrich == "sec_provider_outage":
        return VintageGateResult(
            ok=True,
            ticker=ticker,
            data_period=data_period,
            reason=f"exempt:sec_provider_outage:{err or 'SEC unreachable'}",
            provenance_class=cls.value,
            fiscal_vintage_rule=rule.value,
        )
    return None


def evaluate_vintage_gate(
    ticker: str,
    fields: dict[str, Any] | None,
    *,
    most_recent_reported_period: date | None = None,
    most_recent_earnings_date: date | None = None,  # legacy kw; ignored if period set
    require_load_bearing: bool = True,
    provenance_class: ProvenanceClass | str | None = None,
) -> VintageGateResult:
    """Fail closed on unknown or stale financial provenance (class-aware).

    ``provenance_class`` kwarg is accepted for back-compat but **ignored** for
    classification — class always derives from system-owned
    ``classify_from_fields`` (never from a payload- or caller-supplied label).
    """
    t = (ticker or "").strip().upper()
    payload = dict(fields) if isinstance(fields, dict) else {}
    del most_recent_earnings_date
    del provenance_class  # never trusted — see classify_from_fields

    cls = classify_from_fields(t, payload)
    annotate_provenance_class(t, payload)
    rule = FiscalVintageRule(payload["fiscal_vintage_rule"])

    # --- Class exemptions (named, deliberate) ---------------------------------
    if cls is ProvenanceClass.CASH_TBILL:
        return VintageGateResult(
            ok=True,
            ticker=t,
            reason=f"exempt:{rule.value}",
            provenance_class=cls.value,
            fiscal_vintage_rule=rule.value,
        )
    if cls is ProvenanceClass.FUND_ETF_INDEX:
        return VintageGateResult(
            ok=True,
            ticker=t,
            reason=f"exempt:{rule.value}",
            provenance_class=cls.value,
            fiscal_vintage_rule=rule.value,
        )
    if cls is ProvenanceClass.UNKNOWN:
        return VintageGateResult(
            ok=False,
            ticker=t,
            reason=(
                f"provenance_class_unknown:{t}: instrument could not be "
                "classified; refusing to inherit equity/fund/cash rules"
            ),
            blocked_by="provenance_class_unknown",
            provenance_class=cls.value,
            fiscal_vintage_rule=rule.value,
        )

    # --- single_name_equity: SEC-reported vs data period ----------------------
    data_period = parse_date(payload.get("financials_as_of"))
    reported = most_recent_reported_period or parse_date(
        payload.get("most_recent_reported_period")
    )

    # SEC config / outage outcomes before treating missing period as data fact.
    if reported is None:
        sec_outcome = _sec_enrichment_outcome(
            payload, ticker=t, cls=cls, rule=rule, data_period=data_period,
        )
        if sec_outcome is not None:
            return sec_outcome

    indep = _source_independence_violation(payload)
    if indep:
        return VintageGateResult(
            ok=False,
            ticker=t,
            data_period=data_period,
            most_recent_reported_period=reported,
            reason=indep,
            blocked_by="independence_violation",
            provenance_class=cls.value,
            fiscal_vintage_rule=rule.value,
        )

    if data_period is None:
        return VintageGateResult(
            ok=False,
            ticker=t,
            data_period=None,
            most_recent_reported_period=reported,
            reason=(
                f"provenance_unknown:{t}: missing financials_as_of "
                "(cannot determine which fiscal period the numbers cover)"
            ),
            blocked_by="provenance_unknown",
            provenance_class=cls.value,
            fiscal_vintage_rule=rule.value,
        )

    if reported is None:
        return VintageGateResult(
            ok=False,
            ticker=t,
            data_period=data_period,
            most_recent_reported_period=None,
            reason=(
                f"provenance_unknown:{t}: missing most_recent_reported_period "
                "(cannot determine whether a later quarter has been reported)"
            ),
            blocked_by="provenance_unknown",
            provenance_class=cls.value,
            fiscal_vintage_rule=rule.value,
        )

    stale: list[StaleField] = []
    has_load_bearing = False
    for key in LOAD_BEARING_FINANCIAL_FIELDS:
        if key not in payload or payload.get(key) is None:
            continue
        has_load_bearing = True
        as_of = field_as_of(payload, key) or data_period
        if as_of < reported:
            stale.append(
                StaleField(
                    field=key,
                    as_of=as_of,
                    reported_period=reported,
                    value=payload.get(key),
                )
            )

    if require_load_bearing and not has_load_bearing:
        return VintageGateResult(
            ok=True,
            ticker=t,
            data_period=data_period,
            most_recent_reported_period=reported,
            reason="ok_no_load_bearing_fields",
            provenance_class=cls.value,
            fiscal_vintage_rule=rule.value,
        )

    if data_period < reported and not stale:
        stale.append(
            StaleField(
                field="financials_as_of",
                as_of=data_period,
                reported_period=reported,
                value=data_period.isoformat(),
            )
        )

    if stale or data_period < reported:
        names = ", ".join(sorted({s.field for s in stale})) or "financials_as_of"
        return VintageGateResult(
            ok=False,
            ticker=t,
            data_period=data_period,
            most_recent_reported_period=reported,
            stale_fields=tuple(stale),
            reason=(
                f"vintage_stale:{t}: data period {data_period.isoformat()} "
                f"predates most recent reported period {reported.isoformat()} "
                f"(fields: {names})"
            ),
            blocked_by="vintage_stale",
            provenance_class=cls.value,
            fiscal_vintage_rule=rule.value,
        )

    return VintageGateResult(
        ok=True,
        ticker=t,
        data_period=data_period,
        most_recent_reported_period=reported,
        reason="ok",
        provenance_class=cls.value,
        fiscal_vintage_rule=rule.value,
    )


def evaluate_vintage_gate_for_payload(
    payload: dict[str, dict[str, Any]],
    *,
    ticker: str | None = None,
) -> list[VintageGateResult]:
    if ticker:
        t = ticker.upper()
        return [evaluate_vintage_gate(t, payload.get(t) or payload.get(ticker))]
    return [
        evaluate_vintage_gate(sym, fields)
        for sym, fields in payload.items()
        if isinstance(fields, dict)
    ]


__all__ = [
    "StaleField",
    "VintageGateResult",
    "evaluate_vintage_gate",
    "evaluate_vintage_gate_for_payload",
]
