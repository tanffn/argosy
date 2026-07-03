"""Decision-packet builder — assembles the input the deployment author reasons over
and the deterministic verifier gates against. Pure, no LLM, no DB."""
from __future__ import annotations

from argosy.services.allocation_author.packet import build_decision_packet


class _Inst:
    def __init__(self, symbol, domicile="", weight=0.0):
        self.symbol = symbol
        self.domicile = domicile
        self.weight_within_class_pct = weight


class _Cls:
    def __init__(self, label, target_pct, snapshot_category, instruments):
        self.label = label
        self.target_pct = target_pct
        self.snapshot_category = snapshot_category
        self.instruments = instruments


class _Doc:
    nvda_cap_pct = 30.0

    def __init__(self, classes):
        self.classes = classes


def _doc():
    return _Doc(
        classes=[
            _Cls("Ex-US developed", 15.0, "ex_us", [_Inst("EXUS", "IE")]),
            _Cls("US low-vol", 20.0, "us_equity", [_Inst("SPMV", "IE")]),
            _Cls("Emerging", 8.0, "em", [_Inst("EIMI", "IE")]),
        ]
    )


def test_packet_carries_verifier_fields():
    pkt = build_decision_packet(
        doc=_doc(),
        holdings_usd={"SCHD": 264_000.0, "NVDA": 500_000.0},
        deployable_usd=180_000.0,
    )
    assert pkt["deployable_usd"] == 180_000.0
    assert pkt["holdings"] == {"SCHD": 264_000.0, "NVDA": 500_000.0}
    # known_symbols = plan tickers ∪ holdings, upper-cased (verifier gate).
    assert {"EXUS", "SPMV", "EIMI", "SCHD", "NVDA"} <= pkt["known_symbols"]


def test_plan_menu_structured_with_domicile():
    pkt = build_decision_packet(
        doc=_doc(), holdings_usd={}, deployable_usd=1000.0,
    )
    menu = {m["sleeve"]: m for m in pkt["plan_menu"]}
    assert menu["Ex-US developed"]["target_pct"] == 15.0
    assert menu["Ex-US developed"]["tickers"] == ["EXUS"]
    # domicile carried so the author can prefer UCITS / avoid US-situs.
    assert menu["Ex-US developed"]["domiciles"] == ["IE"]


def test_nvda_concentration_derived():
    pkt = build_decision_packet(
        doc=_doc(),
        holdings_usd={"NVDA": 600_000.0, "SCHD": 400_000.0},
        deployable_usd=1000.0,
        nvda_lookthrough_usd=600_000.0,
    )
    # book defaults to sum(holdings) = 1,000,000 → 60% NVDA.
    assert pkt["nvda"]["pct"] == 60.0
    assert pkt["nvda"]["cap_pct"] == 30.0


def test_reserve_shortfall_never_negative():
    over = build_decision_packet(
        doc=_doc(), holdings_usd={}, deployable_usd=1000.0,
        reserve_target_usd=100_000.0, reserve_current_usd=127_000.0,
    )
    assert over["reserve"]["shortfall_usd"] == 0.0
    short = build_decision_packet(
        doc=_doc(), holdings_usd={}, deployable_usd=1000.0,
        reserve_target_usd=100_000.0, reserve_current_usd=40_000.0,
    )
    assert short["reserve"]["shortfall_usd"] == 60_000.0


def test_instrument_facts_include_lookthrough_for_menu_symbols():
    # FWRA is in the plan menu here; its sourced look-through (≈62% US) must be
    # carried so the author can't treat it as ex-US and the verifier can catch it.
    doc = _Doc(classes=[_Cls("Global", 15.0, "ex_us", [_Inst("FWRA", "IE")])])
    pkt = build_decision_packet(doc=doc, holdings_usd={}, deployable_usd=1000.0)
    facts = {f["symbol"]: f for f in pkt["instrument_facts"]}
    assert "FWRA" in facts
    assert facts["FWRA"]["us_weight"] == 0.62
    assert facts["FWRA"]["source"]


def test_plan_menu_carries_current_and_gap_when_supplied():
    # Canonical current-vs-target attribution → per-sleeve gap the author fills from.
    pkt = build_decision_packet(
        doc=_doc(), holdings_usd={}, deployable_usd=1000.0,
        current_pct_by_sleeve={"Ex-US developed": 3.0, "Emerging": 0.0},
    )
    menu = {m["sleeve"]: m for m in pkt["plan_menu"]}
    # Ex-US: target 15, current 3 → gap +12 (under-target).
    assert menu["Ex-US developed"]["current_pct"] == 3.0
    assert menu["Ex-US developed"]["gap_to_target_pct"] == 12.0
    # A sleeve with no supplied current stays gap-less (no fabricated 0).
    assert "current_pct" not in menu["US low-vol"]


def test_policy_signals_and_constraints_pass_through():
    pkt = build_decision_packet(
        doc=_doc(), holdings_usd={}, deployable_usd=1000.0,
        policy_signals={"nvda_policy_sell": {"due": True, "tranche_usd": 250_000.0}},
        user_constraints="earliest safe retirement; reduce NVDA toward cap",
    )
    assert pkt["policy_signals"]["nvda_policy_sell"]["due"] is True
    assert "retirement" in pkt["user_constraints"]
