"""HouseholdCategorizerAgent: batched LLM categorization with confidence
threshold >= 0.85. Below threshold -> 'uncategorized'.

Refunds (direction='credit' AND tx_type='refund') are filtered out by the
orchestrator before the batch — the matcher inherits their category later.
"""

from __future__ import annotations

import asyncio
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

OUTPUT: Return a JSON object with this exact shape — no extra keys, no prose:
{
  "results": [
    {
      "tx_id": <int>,
      "category_slug": "<slug from taxonomy or 'uncategorized'>",
      "confidence": <float 0.0-1.0>,
      "rationale": "<one sentence>"
    }
  ]
}
Every input tx_id must appear exactly once in results.
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
        """Dispatch to the live LLM via BaseAgent._call_model.

        Patched in unit tests (they never reach this path). The production
        path calls BaseAgent._call_model directly — the same dispatch layer
        used by plan_synthesizer.py and every other Argosy agent — and parses
        the structured JSON response into a CategorizeResponse.

        The call is synchronous: asyncio.run() is safe here because
        categorize_batch is always called from sync contexts (service layer,
        CLI, pytest). Async callers should patch or call _call_model directly.
        """
        system_prompt, user_prompt = self.build_prompt(request=request)
        full_system = self.BOILERPLATE_SYSTEM + "\n\n" + system_prompt

        call = asyncio.run(
            self._call_model(system=full_system, user=user_prompt)
        )

        # Parse the model output — tolerate fenced code blocks.
        cleaned = call.text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

        data = json.loads(cleaned)
        # The LLM returns {"results": [...]}.  Populate metadata from the call.
        results = [CategorizeResult(**r) for r in data["results"]]
        cost = self._estimate_usd(
            tokens_in=call.tokens_in,
            tokens_out=call.tokens_out,
            cache_input_tokens=call.cache_input_tokens,
            cache_creation_tokens=call.cache_creation_tokens,
            thinking_tokens=call.thinking_tokens,
        )
        return CategorizeResponse(
            results=results,
            model=call.model or self.model,
            tokens_in=call.tokens_in,
            tokens_out=call.tokens_out,
            cost_usd=cost,
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
