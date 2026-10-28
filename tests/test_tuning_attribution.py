import copy

from evals.fleet_calibration import tuning_attribution as attribution


def test_formatting_only_not_word_substitutions_or_exponents():
    quote = 'Falsifiers:\n- Revenue below $4.0B or non-GAAP EPS below $7.10.'
    thesis = '**Falsifiers:**\n- Revenue below $4.0B or non-GAAP EPS below $7.10.'
    assert attribution.normalize_quote(quote) in attribution.normalize_quote(thesis)
    assert attribution.normalize_quote('Revenue below $4.1B') not in attribution.normalize_quote(thesis)
    assert attribution.normalize_quote('2**3') == '2**3'
    assert attribution.normalize_quote('2**3 + 4**5') == '2**3 + 4**5'
    assert attribution.normalize_quote('Buy') != attribution.normalize_quote('BUY')


def test_adjudication_preserves_judgments_findings_and_original_failure():
    arm = {"status": "completed", "review_verified": False,
        "grade": {"output": {"thesis_md": "**Falsifiers:**\nRevenue below $4B."}},
        "review": {"output": {"assessable": True, "findings": [
            {"claim_quote": "Falsifiers: Revenue below $4B.", "severity": "minor", "tag": "evidence_omitted"}]}}}
    doc = {"kind": "paired_evidence_fidelity", "status": "completed", "pairs": [
        {"arms": {"excerpt": copy.deepcopy(arm), "complete": copy.deepcopy(arm)}} for _ in range(4)]}
    original = copy.deepcopy(doc)
    result = attribution.adjudicate(doc, "original-hash")
    for pair, old in zip(result["pairs"], original["pairs"], strict=True):
        for key, item in pair["arms"].items():
            assert item["grade"] == old["arms"][key]["grade"]
            assert item["review"] == old["arms"][key]["review"]
            assert item["original_exact_quote_verification"] is False
            assert item["review_verified"] is True
    assert result["summary"]["assessable"] == 4
    assert result["summary"]["conclusion"] == "inconclusive"
