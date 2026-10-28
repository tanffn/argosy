"""Word-preserving attribution adjudication; never reruns or relabels judgments.

Original sealed experiment remains unchanged. This only removes balanced Markdown
bold delimiters and collapses whitespace before checking a quoted substring.
"""
from __future__ import annotations

import argparse
import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

from evals.fleet_calibration.tuning import load_sealed, paired_summary, save_new, seal

ATTRIBUTION_RULE = "exact-or-balanced-markdown-bold-and-whitespace-v2"


def normalize_quote(text):
    text = re.sub(r"(?<![\w$])\*\*(?=\S)(.+?)(?<=\S)\*\*(?!\w)", r"\1", text, flags=re.DOTALL)
    return " ".join(text.split())


def adjudicate(document, source_hash):
    if document.get("kind") != "paired_evidence_fidelity" or document.get("status") != "completed":
        raise ValueError("Completed sealed paired experiment required; no incomplete calls may be repaired")
    document["attribution_adjudication"] = {
        "source_sha256": source_hash, "rule": ATTRIBUTION_RULE,
        "policy_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "adjudicated_at": datetime.now(UTC).isoformat(),
        "scope": "No words, grader views, findings, severities, assay assessments or model receipts change. Only quoted-substring attribution ignores balanced bold markup and whitespace."}
    for pair in document["pairs"]:
        for arm in pair["arms"].values():
            if arm.get("status") != "completed":
                continue
            original = arm["review_verified"]
            original_thesis = arm["grade"]["output"]["thesis_md"]
            thesis = normalize_quote(original_thesis)
            checks = [{"claim_quote": f["claim_quote"],
                       "exact_match": bool(f["claim_quote"]) and f["claim_quote"] in original_thesis,
                       "normalized_match": bool(normalize_quote(f["claim_quote"]))
                       and normalize_quote(f["claim_quote"]) in thesis}
                      for f in arm["review"]["output"]["findings"]]
            arm["original_exact_quote_verification"] = original
            arm["attribution_checks"] = checks
            arm["review_verified"] = all(r["exact_match"] or r["normalized_match"] for r in checks)
    document["summary"] = paired_summary(document)
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists() or args.out.with_suffix(".seal.json").exists():
        parser.error("Write-once output required; original experiment never overwritten")
    document, source_hash = load_sealed(args.source)
    adjudicate(document, source_hash)
    save_new(args.out, document)
    seal(args.out, "completed")
    print(document["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
