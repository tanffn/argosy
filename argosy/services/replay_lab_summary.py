"""Read-only UI projection of the side-track lab; never promotes its decisions."""
from __future__ import annotations

from datetime import date
import hashlib
import json
import math
from pathlib import Path


def latest_lab_summary(root: Path) -> dict | None:
    from evals.fleet_calibration.lab import digest, summarize
    from evals.fleet_calibration.market_outcomes import compare_horizons, validate_identity

    seals = sorted((root / "runs" / "lab").glob("*.lab.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not seals:
        return None
    seal_path = seals[0]
    run_path = seal_path.with_name(seal_path.name.removesuffix(".lab.json") + ".json")
    try:
        raw = run_path.read_bytes()
        seal = json.loads(seal_path.read_text(encoding="utf-8"))
        run_hash = hashlib.sha256(raw).hexdigest()
        if seal.get("run_sha256") != run_hash:
            raise ValueError("Run changed after sealing")
        document = json.loads(raw)
        result = summarize(document)
        result["run_id"] = run_path.stem
        result["finished_at"] = document.get("finished")
        packets = {p["packet"]["case_id"]: p["packet"] for p in document["lab_manifest"]["cases"]}
        by_id = {row["case_id"]: row for row in result["cases"]}
        for path in sorted(run_path.parent.glob(run_path.stem + ".*.outcomes.json")):
            try:
                evidence_raw = path.read_bytes()
                evidence_seal = json.loads(path.with_suffix(".sha256").read_text(encoding="utf-8"))
                if evidence_seal.get("sha256") != hashlib.sha256(evidence_raw).hexdigest():
                    raise ValueError("Outcome evidence changed after sealing")
                outcome = json.loads(evidence_raw)
                cid = outcome["case_id"]
                if (outcome["run_sha256"] != run_hash or cid not in packets or
                        outcome["packet_sha256"] != digest(packets[cid]) or
                        not by_id[cid]["qualified"] or not result["controls_passed"]):
                    raise ValueError("Outcome is not linked to this qualified frozen decision")
                if outcome["benchmark"] != "SPY" or outcome["freeze_date"] != packets[cid]["freeze_date"]:
                    raise ValueError("Outcome benchmark or freeze date changed")
                validate_identity(packets[cid], outcome["symbol"])
                evidence = outcome["price_evidence"]
                def decode_prices(values):
                    if not isinstance(values, dict) or any(type(p) not in (int, float) or not math.isfinite(p) or p <= 0 for p in values.values()):
                        raise ValueError("Prices must be finite positive JSON numbers; no bool/string coercion")
                    return {date.fromisoformat(d): p for d, p in values.items()}
                subject = decode_prices(evidence[outcome["symbol"]])
                benchmark = decode_prices(evidence["SPY"])
                by_id[cid]["market_horizons"] = compare_horizons(
                    freeze=date.fromisoformat(outcome["freeze_date"]), subject=subject,
                    benchmark=benchmark, as_of=date.fromisoformat(outcome["as_of"]))
                by_id[cid]["market_errors"] = outcome.get("errors", [])
            except (AttributeError, KeyError, ValueError, TypeError, OSError) as exc:
                result.setdefault("outcome_errors", []).append(f"{path.name}: {exc}")
        return result
    except (AttributeError, KeyError, TypeError, ValueError, OSError) as exc:
        return {"status": "invalid", "run_id": run_path.stem, "controls_passed": False,
                "cases": [], "limitations": [f"Replay artifacts could not be verified: {exc}"]}
