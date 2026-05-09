"""HouseholdCategorizerAgent: batched LLM categorization with confidence
threshold >= 0.85. Below threshold -> 'uncategorized'.

Refunds (direction='credit' AND tx_type='refund') are filtered out by the
orchestrator before the batch — the matcher inherits their category later.
"""

from __future__ import annotations

import json

from argosy.agents.base import BaseAgent
from argosy.agents.household_categorizer_types import (
    CategorizeRequest, CategorizeResponse, CategorizeResult, CategorizeRow,
)

CONFIDENCE_THRESHOLD = 0.85


SYSTEM_PROMPT = """You are the household-budget categorizer on the Argosy fleet.
The user runs a household in Israel. You categorize each transaction into ONE
slug from the taxonomy provided, or return 'uncategorized'.

Rules:
- If you are not at least 0.85 confident, return 'uncategorized'. Do NOT guess.
- Refunds should not normally appear; if one does, return 'uncategorized' with
  rationale='refund — should be matched to prior purchase'.
- issuer_category_he is a hint, not gospel. Override when wrong.
- Foreign merchants: use post-prefix substring (PAYPAL *X -> X).
"""


class HouseholdCategorizerAgent(BaseAgent):
    agent_role = "household_categorizer"
    require_citations = False

    def categorize_batch(
        self, rows: list[CategorizeRow], taxonomy: list[str],
    ) -> list[CategorizeResult]:
        request = CategorizeRequest(transactions=rows, taxonomy=taxonomy)
        response = self._invoke_llm(request)
        thresholded: list[CategorizeResult] = []
        for r in response.results:
            if r.confidence < CONFIDENCE_THRESHOLD:
                thresholded.append(CategorizeResult(
                    tx_id=r.tx_id, category_slug="uncategorized",
                    confidence=r.confidence,
                    rationale=f"below-threshold ({r.rationale})",
                ))
            else:
                thresholded.append(r)
        return thresholded

    def _invoke_llm(self, request: CategorizeRequest) -> CategorizeResponse:
        """Indirection seam — patched in unit tests. Production path calls
        the real BaseAgent structured-output via build_prompt + _call_model.
        Mirror the existing pattern in argosy/agents/plan_synthesizer.py.

        NOTE: NotImplementedError here is intentional for now. Unit tests
        patch this method directly. The live LLM eval (Task 28) will wire
        this up using BaseAgent.run_sync / build_prompt, at which point the
        raise below should be replaced with the real dispatch.
        """
        raise NotImplementedError(
            "Wire up via BaseAgent's structured-output method; mirror "
            "plan_synthesizer.py. The unit tests patch this method, so a "
            "raise here is fine for now and the live LLM eval (Task 28) "
            "will exercise the real wiring."
        )

    def build_prompt(self, **inputs) -> tuple[str, str]:
        """Build (system, user) for use when wiring _invoke_llm to BaseAgent.run."""
        request: CategorizeRequest = inputs["request"]
        user_prompt = self._build_user_prompt(request)
        return SYSTEM_PROMPT, user_prompt

    def _build_user_prompt(self, request: CategorizeRequest) -> str:
        tx_lines = []
        for r in request.transactions:
            tx_lines.append(json.dumps({
                "tx_id": r.tx_id,
                "merchant_normalized": r.merchant_normalized,
                "merchant_raw": r.merchant_raw,
                "amount_nis": r.amount_nis,
                "direction": r.direction,
                "occurred_on": r.occurred_on.isoformat(),
                "issuer_kind": r.issuer_kind,
                "issuer_name": r.issuer_name,
                "issuer_category_he": r.issuer_category_he,
            }, ensure_ascii=False))
        return (
            "<taxonomy>\n" + "\n".join(request.taxonomy) + "\n</taxonomy>\n\n"
            "<transactions>\n[\n" + ",\n".join(tx_lines) + "\n]\n</transactions>"
        )


__all__ = ["HouseholdCategorizerAgent"]
