"""Only explicitly declared work stages propagate nested failures."""
import pytest

from argosy.services.jobs.summary_status import derive_run_status


@pytest.mark.parametrize("summary", [
    {"stages": {"prices": {"adapter_errors": 1}}},
    {"stages": {"prices": {"errors": ["unavailable"]}}},
    {"stages": {"nested": {"stages": {"prices": {"status": "failed"}}}}},
    {"stages": ["malformed"]},
    {"stages": {"prices": None}},
])
def test_stage_failures_reach_run_status(summary):
    status, reason = derive_run_status(summary)
    assert status == "error"
    assert reason


def test_unrelated_payload_and_unscorable_coverage_are_not_operational_errors():
    summary = {"report": {"errors": ["quoted business claim"]},
               "stages": {"prices": {"adapter_errors": 0},
                          "scores": {"still_unparseable": 12}}}
    assert derive_run_status(summary) == ("ok", None)


@pytest.mark.parametrize("ingest,analyze,expected", [
    ("ok", "ok", "ok"), ("ok", "skipped", "ok"),
    ("error", "pending", "error"), ("ok", "error", "error"),
    ("ok", "pending", "error"), ("ok", "mystery", "error"),
])
def test_real_news_summary_status_strings(ingest, analyze, expected):
    from argosy.services.jobs.news_daily import _build_summary

    summary = _build_summary(stage1_result=None, stage2_result=None,
                             stage1_status=ingest, stage2_status=analyze,
                             stage1_error=None, stage2_error=None)
    assert derive_run_status(summary)[0] == expected


def test_healthy_stage_strings_do_not_hide_error_counts_or_nested_failures():
    assert derive_run_status({"stages": {"ingest": "ok", "analyze": "skipped"},
                              "error_count": 1})[0] == "error"
    assert derive_run_status({"stages": {"ingest": "ok", "nested": {
        "stages": {"analysis": "error"}}}})[0] == "error"


@pytest.mark.parametrize("stage", ["evaluator", "reevaluation", "benchmark"])
def test_real_evaluator_summary_serializers_propagate_adapter_failures(stage):
    from argosy.services.predictions.benchmark import BenchmarkSummary
    from argosy.services.predictions.evaluator import EvaluatorSummary, ReevaluationSummary

    summaries = {"evaluator": EvaluatorSummary(), "reevaluation": ReevaluationSummary(),
                 "benchmark": BenchmarkSummary()}
    summaries[stage].adapter_errors = 1
    status, reason = derive_run_status({"stages": {k: v.to_dict() for k, v in summaries.items()}})
    assert status == "error"
    assert stage in reason
