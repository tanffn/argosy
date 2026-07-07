"""Tests for the FM first-greeting assembly (GET /api/home/greeting).

The classifier tests run over the FIVE real flags that were live on the
dev DB when this shipped (ids 1 / 73 / 79 / 85 / 86) — payload excerpts
copied verbatim so the classification contract is pinned to reality:

* id 1  — 38-day-old ``alpha_report_caution`` → excluded as expired
          (post-backfill it carries a past ``expires_at``);
* id 73 — fx-feed blind spot → internal (Argosy's own data gap);
* id 79 — expense-ingestion gap → internal (payload semantics);
* id 85 — NKE thesis weakened → watching;
* id 86 — USD cash flipped negative post-deploy → watching, with the
          "resolves with the next broker export" note when the
          closed-loop needs-you item is present.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from argosy.services.home_greeting import (
    BUCKET_INTERNAL,
    BUCKET_NEEDS_YOU,
    BUCKET_SKIP,
    BUCKET_WATCHING,
    classify_flag,
    classify_proposal,
    select_active_flags,
    watching_note,
)
from argosy.state.models import ActionProposal, MonitorFlag, PortfolioSnapshotRow, User

NOW = datetime(2026, 7, 7, 10, 0, 0, tzinfo=timezone.utc)

# --- Real payloads (dev DB, 2026-07-07) -----------------------------------

FLAG_73_FX = {
    "kind": "state_observer_fx_observation",
    "severity": "warning",
    "payload": {
        "primary_field": "macro.fx_usd_nis_spot",
        "related_fields": ["macro.fx_usd_nis_30d_avg"],
        "rationale_md": (
            "The plan is denominated in USD/NIS (assumed 2.94161) and the "
            "overwhelming majority of net worth sits in USD assets, so the FX "
            "rate is the single most impactful conversion input to every "
            "retirement figure. With no live spot or 30-day feed we cannot "
            "detect drift in that variable at all — this is a blind spot on "
            "the plan's most consequential assumption, not a confirmed deviation."
        ),
    },
}

FLAG_79_EXPENSE_GAP = {
    "kind": "state_observer_cashflow_observation",
    "severity": "warning",
    "payload": {
        "primary_field": "cashflow_recent.last_3_months[1].realized_expense_nis",
        "related_fields": [],
        "rationale_md": (
            "A full calendar month (June) shows zero realized household "
            "expenses against a 23,084 NIS plan assumption, which is "
            "implausible for this household and points to an expense-ingestion "
            "gap rather than genuinely near-zero spending."
        ),
    },
}

FLAG_85_NKE = {
    "kind": "thesis_monitor_weakened",
    "severity": "warning",
    "payload": {
        "ticker": "NKE",
        "thesis_status": "weakened",
        "suggested_action": "watchlist",
        "rationale_md": (
            "Non-plan holding being redeployed, but the feed shows "
            "**sustained fundamental deterioration**: persistent multi-year "
            "earnings deceleration. Worth a watch given the redeployment context."
        ),
        # verbatim from dev-DB flag id 85 (2026-07-07)
        "signals": [
            "multi-year earnings deceleration",
            "-44% off 52w high",
            "turnaround delayed; dividend not cut",
            "non-plan, being redeployed",
        ],
    },
}

FLAG_86_CASH_DRAWDOWN = {
    "kind": "state_observer_position_observation",
    "severity": "warning",
    "payload": {
        "primary_field": "portfolio.positions[9].value_usd",
        "related_fields": [
            "portfolio.positions[9].value_nis",
            "portfolio.cash_balances_usd",
            "portfolio.unallocated_cash_usd",
        ],
        "rationale_md": (
            "A USD cash account flipped from a large positive balance into "
            "negative territory at the same time total cash was drawn down "
            "~83%, which points to an overdraft or over-deployment rather "
            "than a routine drawdown."
        ),
        # verbatim from dev-DB flag id 86 (2026-07-07)
        "mitigation_hint": (
            "Review /portfolio cash reconciliation; confirm the negative USD "
            "line is intended (settlement float) vs an accidental overdraft."
        ),
        "deviation_bucket": "extreme",
    },
}

#: The dev-DB book's negative cash line (snapshot 12, Leumi USD −$16.43k) in
#: the resolved shape ``_negative_cash_lines`` emits.
NEG_CASH_LEUMI = {
    "location": "Leumi",
    "currency": "USD",
    "usd": -16434.66,
    "snapshot_date": "2026-07-07",
}


class TestClassifyFlag:
    def test_fx_blind_spot_is_internal(self):
        f = FLAG_73_FX
        assert classify_flag(f["kind"], f["severity"], f["payload"]) == BUCKET_INTERNAL

    def test_expense_ingestion_gap_is_internal(self):
        f = FLAG_79_EXPENSE_GAP
        assert classify_flag(f["kind"], f["severity"], f["payload"]) == BUCKET_INTERNAL

    def test_nke_thesis_weakened_is_watching(self):
        f = FLAG_85_NKE
        assert classify_flag(f["kind"], f["severity"], f["payload"]) == BUCKET_WATCHING

    def test_cash_drawdown_is_watching(self):
        f = FLAG_86_CASH_DRAWDOWN
        assert classify_flag(f["kind"], f["severity"], f["payload"]) == BUCKET_WATCHING

    def test_info_severity_is_skipped(self):
        # id 87-style: intended-deployment observation, severity info.
        assert (
            classify_flag(
                "state_observer_cash_observation",
                "info",
                {"rationale_md": "Cash buffer thin against the 5% target."},
            )
            == BUCKET_SKIP
        )

    def test_suggested_client_action_is_needs_you(self):
        assert (
            classify_flag(
                "thesis_monitor_broken",
                "critical",
                {"rationale_md": "Thesis broke.", "suggested_action": "needs_confirm"},
            )
            == BUCKET_NEEDS_YOU
        )


class TestWatchingNote:
    def test_cash_drawdown_links_to_broker_export_when_closed_loop_open(self):
        f = FLAG_86_CASH_DRAWDOWN
        note = watching_note(f["kind"], f["payload"], has_closed_loop_needs_you=True)
        assert "broker export" in note

    def test_cash_drawdown_plain_note_without_closed_loop(self):
        f = FLAG_86_CASH_DRAWDOWN
        note = watching_note(f["kind"], f["payload"], has_closed_loop_needs_you=False)
        assert "No action needed" in note
        assert "broker export" not in note

    def test_thesis_flag_gets_plain_no_action_note(self):
        f = FLAG_85_NKE
        note = watching_note(f["kind"], f["payload"], has_closed_loop_needs_you=True)
        assert "No action needed" in note
        assert "broker export" not in note


class TestClassifyProposal:
    def test_closed_loop_unverified_is_needs_you(self):
        assert (
            classify_proposal("note_only", "closed_loop_unverified:ariel", "proposed")
            == BUCKET_NEEDS_YOU
        )

    def test_period_directive_allocate_is_needs_you(self):
        assert (
            classify_proposal("allocate", "period_directive:ariel", "proposed")
            == BUCKET_NEEDS_YOU
        )

    def test_holistic_rebalance_is_needs_you(self):
        assert (
            classify_proposal(
                "rebalance", "v1|rebalance|holistic_rebalance|critical", "proposed"
            )
            == BUCKET_NEEDS_YOU
        )

    def test_flagsig_rebalance_chatter_is_skipped(self):
        assert (
            classify_proposal(
                "rebalance",
                "v1|rebalance|flagsig:state_observer_allocation_observation:x|warning",
                "proposed",
            )
            == BUCKET_SKIP
        )

    def test_note_only_and_watchlist_chatter_skipped(self):
        assert classify_proposal("note_only", "v1|note_only|flagsig:x|warning", "proposed") == BUCKET_SKIP
        assert classify_proposal("set_watchlist", "v1|set_watchlist|flagsig:x|warning", "proposed") == BUCKET_SKIP

    def test_accepted_pending_user_action_is_needs_you(self):
        assert (
            classify_proposal("note_only", None, "accepted_pending_user_action")
            == BUCKET_NEEDS_YOU
        )


class TestHeadlineForFlag:
    """The greeting headline must CARRY the facts (which account, amount,
    signals, likely cause) — not a vague 'a USD cash account flipped'."""

    def test_cash_flag_headline_names_account_amount_and_cause(self):
        from argosy.services.home_greeting import headline_for_flag

        f = FLAG_86_CASH_DRAWDOWN
        line = headline_for_flag(
            f["kind"], f["payload"], negative_cash_lines=[NEG_CASH_LEUMI]
        )
        assert "Leumi" in line
        assert "USD" in line
        assert "-$16.4k" in line
        assert "~83%" in line
        assert "over-deployment" in line

    def test_cash_flag_falls_back_when_no_negative_line_in_book(self):
        from argosy.services.home_greeting import headline_for_flag

        f = FLAG_86_CASH_DRAWDOWN
        line = headline_for_flag(f["kind"], f["payload"], negative_cash_lines=[])
        # already resolved in the book -> generic first-sentence fallback
        assert line.startswith("A USD cash account flipped")

    def test_thesis_flag_headline_leads_with_signals(self):
        from argosy.services.home_greeting import headline_for_flag

        f = FLAG_85_NKE
        line = headline_for_flag(f["kind"], f["payload"])
        assert line.startswith("NKE thesis weakened")
        assert "multi-year earnings deceleration" in line
        assert "-44% off 52w high" in line

    def test_thesis_flag_without_signals_falls_back_to_first_sentence(self):
        from argosy.services.home_greeting import headline_for_flag

        payload = {k: v for k, v in FLAG_85_NKE["payload"].items() if k != "signals"}
        line = headline_for_flag(FLAG_85_NKE["kind"], payload)
        assert line.startswith("NKE thesis weakened:")

    def test_unknown_kind_uses_generic_line(self):
        from argosy.services.home_greeting import headline_for_flag

        f = FLAG_73_FX
        line = headline_for_flag(f["kind"], f["payload"])
        assert line.startswith("The plan is denominated")

    def test_fmt_usd_clean_rounding(self):
        from argosy.services.home_greeting import _fmt_usd

        assert _fmt_usd(-16434.66) == "-$16.4k"
        assert _fmt_usd(9605.34) == "$9.6k"
        assert _fmt_usd(2_243_154.0) == "$2.2M"
        assert _fmt_usd(500.0) == "$500"


# --- DB-backed: selection + assembly ---------------------------------------


def _seed_user(SF, user_id: str = "ariel") -> None:
    with SF() as s:
        if s.get(User, user_id) is None:
            s.add(User(id=user_id))
            s.commit()


def _seed_flag(SF, *, kind, severity, payload, surfaced_at, expires_at, dedup_key):
    with SF() as s:
        f = MonitorFlag(
            user_id="ariel",
            kind=kind,
            severity=severity,
            payload=json.dumps(payload),
            surfaced_at=surfaced_at,
            expires_at=expires_at,
            dedup_key=dedup_key,
        )
        s.add(f)
        s.commit()
        return f.id


def _seed_proposal(SF, *, kind, dedup_key, summary, severity="warning"):
    with SF() as s:
        p = ActionProposal(
            user_id="ariel",
            summary=summary,
            rationale_md=f"why: {summary}",
            suggested_payload="{}",
            severity=severity,
            expires_at=(NOW + timedelta(days=7)).replace(tzinfo=None),
            kind=kind,
            dedup_key=dedup_key,
        )
        s.add(p)
        s.commit()
        return p.id


class TestSelectActiveFlags:
    def test_expired_backfilled_caution_is_excluded(self, client_with_db):
        """id-1 shape: 38-day-old caution whose backfilled expiry is past."""
        SF = client_with_db.app.state.session_factory
        _seed_user(SF)
        surfaced = (NOW - timedelta(days=38)).replace(tzinfo=None)
        _seed_flag(
            SF,
            kind="alpha_report_caution",
            severity="warning",
            payload={"caution": "MSFT losing 400 is an early warning sign for QQQ"},
            surfaced_at=surfaced,
            expires_at=surfaced + timedelta(days=14),
            dedup_key="v1|alpha_report_caution|10.eb9986947db",
        )
        live_id = _seed_flag(
            SF,
            kind=FLAG_85_NKE["kind"],
            severity="warning",
            payload=FLAG_85_NKE["payload"],
            surfaced_at=NOW.replace(tzinfo=None),
            expires_at=(NOW + timedelta(days=7)).replace(tzinfo=None),
            dedup_key="v1|thesis_monitor|ariel|NKE|weakened",
        )
        with SF() as s:
            rows = select_active_flags(s, "ariel", now=NOW)
        assert [r.id for r in rows] == [live_id]


class TestGreetingEndpoint:
    def _seed_book(self, SF) -> None:
        with SF() as s:
            s.add(
                PortfolioSnapshotRow(
                    user_id="ariel",
                    imported_at=NOW.replace(tzinfo=None),
                    positions_json=json.dumps(
                        [
                            {
                                "location": "Leumi",
                                "currency": "USD",
                                "asset_type": "Cash",
                                "symbol": "",
                                "usd_value_k": -16.43466,
                            },
                        ]
                    ),
                    totals_json=json.dumps(
                        {"total_usd_value_k": 3999.279, "cash_balances_usd_k": 29.7}
                    ),
                )
            )
            s.commit()

    def test_happy_path_shapes_and_buckets(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        _seed_user(SF)
        self._seed_book(SF)
        # needs_you sources: the closed-loop export request + the deploy directive.
        _seed_proposal(
            SF,
            kind="note_only",
            dedup_key="closed_loop_unverified:ariel",
            summary="I placed 11 fills — send me the broker export so I can verify the book.",
        )
        _seed_proposal(
            SF,
            kind="allocate",
            dedup_key="period_directive:ariel",
            summary="Deploy ~$98k idle cash: EXUS $19k, CSPX $18k ...",
            severity="info",
        )
        # Chatter that must NOT surface.
        _seed_proposal(
            SF,
            kind="note_only",
            dedup_key="v1|note_only|flagsig:state_observer_cashflow_observation:x|warning",
            summary="June shows 0 NIS realized household expenses.",
        )
        # Flags: internal (fx), watching (NKE), watching+export-note (cash).
        for spec, dedup in (
            (FLAG_73_FX, "v1|state_observer|ariel|fx_observation|x|large"),
            (FLAG_85_NKE, "v1|thesis_monitor|ariel|NKE|weakened"),
            (FLAG_86_CASH_DRAWDOWN, "v1|state_observer|ariel|position_observation|x|extreme"),
        ):
            _seed_flag(
                SF,
                kind=spec["kind"],
                severity=spec["severity"],
                payload=spec["payload"],
                surfaced_at=NOW.replace(tzinfo=None),
                expires_at=(NOW + timedelta(days=7)).replace(tzinfo=None),
                dedup_key=dedup,
            )

        r = client_with_db.get("/api/home/greeting?user_id=ariel")
        assert r.status_code == 200
        body = r.json()

        assert body["greeting_name"] == "Ariel"
        assert body["book"]["total_usd"] == 3999279.0
        assert isinstance(body["book"]["on_plan"], bool)
        assert body["book"]["fi_line"].startswith("FI track")

        needs_ids = {i["id"] for i in body["needs_you"]}
        assert len(body["needs_you"]) == 2
        assert all(i["cta"]["label"] and i["cta"]["href"] for i in body["needs_you"])
        assert any(
            i["cta"]["label"] == "Send the broker export" for i in body["needs_you"]
        )
        assert any(i["kind"] == "allocate" for i in body["needs_you"])
        # why is one click away — the rationale rides along.
        assert all(i["why_md"] for i in body["needs_you"])

        # watching: NKE + cash drawdown; fx internal flag excluded.
        assert len(body["watching"]) == 2
        cash_line = next(
            w for w in body["watching"] if "cash" in w["headline"].lower()
        )
        assert "broker export" in cash_line["note"]
        # The headline carries the FACTS: which account, the amount, the cause.
        assert "Leumi" in cash_line["headline"]
        assert "-$16.4k" in cash_line["headline"]
        assert "over-deployment" in cash_line["headline"]
        nke_line = next(w for w in body["watching"] if "NKE" in w["headline"])
        assert "No action needed" in nke_line["note"]
        assert "-44% off 52w high" in nke_line["headline"]

        assert body["quiet"] is False
        assert "next_review_local" in body

    def test_empty_book_fallback(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        _seed_user(SF)
        r = client_with_db.get("/api/home/greeting?user_id=ariel")
        assert r.status_code == 200
        body = r.json()
        assert body["book"]["total_usd"] is None
        assert body["book"]["on_plan"] is False
        assert body["book"]["on_plan_note"] == "no portfolio snapshot yet"
        assert body["needs_you"] == []
        assert body["watching"] == []
        assert body["quiet"] is True
