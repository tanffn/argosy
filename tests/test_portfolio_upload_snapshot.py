"""Tests for POST /api/portfolio/upload-snapshot.

The route accepts a Family Finances Status TSV, validates it with
``parse_portfolio_tsv``, persists it under the windfall-detector scan
root, and (by default) fires the detector synchronously.

Coverage:
  - Happy path: TSV persists; detector skipped on first upload (no
    previous TSV).
  - Detector ok + windfall fires when previous TSV exists with smaller
    cash position.
  - Rejection when the upload is missing the header marker.
  - SHA stable across identical uploads.
  - fire_detector=false suppresses the detector run.

The XLS-to-TSV conversion is NOT exercised here -- the route accepts
TSVs directly. Porting the user's external `update_leumi_tsv.py` into
the repo is queued for a follow-up session.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


def _minimal_tsv(
    *,
    leumi_usd_cash: float,
    leumi_nis_cash: float,
    snapshot_date: str = "24-Mar-26",
    fx: float = 2.94,
) -> str:
    return (
        f"\t{snapshot_date}\t\n"
        f"\tUSD to NIS:\t{fx}\n"
        f"\tUSD to EUR:\t0.85\n"
        f"\n"
        f"Bank account / funds allocation\n"
        f"Review Status\tLocation\tCurrency\tType\tDetails\tSymbol\t# Shares\tCurrent price\tAvg Price\tCurrent Value\t(K) USD Value\t% Change\t% Yearly\n"
        f"\tschwab\tUSD\tNVIDIA\tRSU\tNVDA\t11471\t200.14\t200.14\t2,295,805\t2295\t0%\t\n"
        f"\tLeumi\tNIS\tCash\tCash\t\t{int(leumi_nis_cash)}\t1\t1\t{leumi_nis_cash:,.0f}\t{int(leumi_nis_cash/fx/1000)}\t0%\t\n"
        f"\tLeumi\tUSD\tCash\tCash\t\t{int(leumi_usd_cash)}\t1\t1\t{leumi_usd_cash:,.0f}\t{int(leumi_usd_cash/1000)}\t0%\t\n"
        f"\n"
        f"Current allocation:\n"
        f"\tType\tSUM of (K) USD Value\tSUM of (K) USD Value\tTargetPct\tTargetK\tDelta (K) USD\t\n"
        f"\tCash\t13%\t188\t5%\t72.7\t-115.4\t\n"
        f"\tCore Equity\t26%\t381\t20%\t290.6\t-90.8\t\n"
        f"\tGrowth\t11%\t158\t20%\t290.6\t132.2\t\n"
        f"\tGrand Total\t100%\t1453\t100%\t1453.2\t0.0\t\n"
    )


@pytest.fixture
def snapshot_root(tmp_path, monkeypatch):
    """Point ARGOSY_EXPENSE_SAMPLES_ROOT at a tmp dir so the upload
    route writes there + the detector scans there. Returns the Path."""
    root = tmp_path / "snapshots"
    root.mkdir()
    monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(root))
    return root


class TestUploadSnapshot:
    def test_happy_path_first_upload(self, client_with_db, snapshot_root):
        """First upload: persists, detector skipped (no previous TSV)."""
        body = _minimal_tsv(leumi_usd_cash=50_000, leumi_nis_cash=80_000)
        files = {
            "file": (
                "Family Finances Status - 26 Mar.tsv",
                body.encode("utf-8"),
                "text/tab-separated-values",
            ),
        }
        data = {"user_id": "ariel"}
        resp = client_with_db.post(
            "/api/portfolio/upload-snapshot", data=data, files=files,
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["tsv_persisted"] is True
        assert payload["detect_status"] == "skipped"
        assert payload["event"] is None
        # Persisted under the snapshot root with the canonical name.
        persisted = Path(payload["persisted_path"])
        assert persisted.exists()
        assert persisted.parent == snapshot_root
        assert "Family Finances Status" in persisted.name
        # SHA stable: same content -> same hash.
        sha1 = payload["sha256"]
        assert len(sha1) == 64

    def test_detector_fires_when_prev_exists(
        self, client_with_db, snapshot_root,
    ):
        """Two snapshots, $100K USD cash delta between them -> event fires."""
        prev_body = _minimal_tsv(
            leumi_usd_cash=55_000, leumi_nis_cash=80_000,
            snapshot_date="24-Feb-26",
        )
        cur_body = _minimal_tsv(
            leumi_usd_cash=155_000, leumi_nis_cash=80_000,
            snapshot_date="24-Mar-26",
        )

        # Upload prev first (sets up the "previous TSV").
        r1 = client_with_db.post(
            "/api/portfolio/upload-snapshot",
            data={"user_id": "ariel"},
            files={
                "file": (
                    "Family Finances Status - 26 Feb.tsv",
                    prev_body.encode("utf-8"),
                    "text/tab-separated-values",
                ),
            },
        )
        assert r1.status_code == 200
        assert r1.json()["detect_status"] == "skipped"

        # Upload current.
        r2 = client_with_db.post(
            "/api/portfolio/upload-snapshot",
            data={"user_id": "ariel"},
            files={
                "file": (
                    "Family Finances Status - 26 Mar.tsv",
                    cur_body.encode("utf-8"),
                    "text/tab-separated-values",
                ),
            },
        )
        assert r2.status_code == 200, r2.text
        payload = r2.json()
        assert payload["tsv_persisted"] is True
        assert payload["detect_status"] == "ok"
        assert payload["event"] is not None
        assert payload["event"]["cash_delta_usd"] == 100_000.0
        assert payload["plan"] is not None
        assert payload["plan"]["windfall_usd"] > 0

    def test_rejects_non_portfolio_tsv(self, client_with_db, snapshot_root):
        """A TSV without the canonical header marker is rejected."""
        body = "this is not a portfolio tsv\njust some text\n"
        resp = client_with_db.post(
            "/api/portfolio/upload-snapshot",
            data={"user_id": "ariel"},
            files={
                "file": ("random.tsv", body.encode("utf-8"), "text/plain"),
            },
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["tsv_persisted"] is False
        assert payload["detect_status"] == "skipped"
        assert "header marker" in (payload["detail"] or "").lower()
        # Even rejected uploads return a SHA so the client can dedupe.
        assert len(payload["sha256"]) == 64

    def test_sha_stable_for_identical_uploads(
        self, client_with_db, snapshot_root,
    ):
        """Two uploads of the same bytes produce the same SHA."""
        body = _minimal_tsv(leumi_usd_cash=50_000, leumi_nis_cash=80_000)
        files = {
            "file": ("snap.tsv", body.encode("utf-8"), "text/plain"),
        }
        data = {"user_id": "ariel"}
        r1 = client_with_db.post(
            "/api/portfolio/upload-snapshot", data=data, files=files,
        )
        r2 = client_with_db.post(
            "/api/portfolio/upload-snapshot",
            data=data,
            files={"file": ("snap.tsv", body.encode("utf-8"), "text/plain")},
        )
        assert r1.json()["sha256"] == r2.json()["sha256"]

    def test_fire_detector_false_skips_detection(
        self, client_with_db, snapshot_root,
    ):
        """fire_detector=false bypasses the detector entirely."""
        prev_body = _minimal_tsv(
            leumi_usd_cash=55_000, leumi_nis_cash=80_000,
        )
        cur_body = _minimal_tsv(
            leumi_usd_cash=200_000, leumi_nis_cash=80_000,
        )
        client_with_db.post(
            "/api/portfolio/upload-snapshot",
            data={"user_id": "ariel"},
            files={"file": (
                "Family Finances Status - 26 Feb.tsv",
                prev_body.encode("utf-8"), "text/plain",
            )},
        )
        resp = client_with_db.post(
            "/api/portfolio/upload-snapshot",
            data={"user_id": "ariel", "fire_detector": "false"},
            files={"file": (
                "Family Finances Status - 26 Mar.tsv",
                cur_body.encode("utf-8"), "text/plain",
            )},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["tsv_persisted"] is True
        assert payload["detect_status"] == "skipped"
        assert payload["event"] is None
