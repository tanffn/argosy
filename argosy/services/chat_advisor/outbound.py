"""Single deterministic privacy and Discord-length boundary."""

from __future__ import annotations

import re
from collections.abc import Sequence

from argosy.services.chat_advisor.contracts import Citation

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_ -]?key|token|secret|password|authorization)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"\b(?:mfa\.)?[A-Za-z\d_-]{20,}\.[A-Za-z\d_-]{6,}\.[A-Za-z\d_-]{20,}\b"),
    re.compile(r"\b(?:sk-ant-|sk-proj-|ghp_|github_pat_|xox[baprs]-)[A-Za-z\d_-]{8,}\b"),
)
_QUOTED_PATH_PATTERN = re.compile(r"([\"'])(?:[A-Za-z]:[\\/]|/)[^\r\n\"']+\1")
_PATH_PATTERN = re.compile(r"(?<![:/\w])(?:[A-Za-z]:[\\/]|/)(?:[^\s<>|\"']+[\\/])*[^\s<>|\"']+")
_ACCOUNT_PATTERN = re.compile(
    r"(?i)([\"']?account(?:[_ -]?(?:id|number|no\.?))?[\"']?\s*[:=#]\s*[\"']?)[A-Za-z\d*-]{4,}([\"']?)"
)
_COMMON_ACCOUNT_PATTERN = re.compile(r"\b(?:U\d{5,10}|[A-Z]{2}\d{2}[A-Z0-9]{11,30})\b")
_MENTION_PATTERN = re.compile(r"@(everyone|here)|<@&?\d+>|<@!\d+>", re.IGNORECASE)


class OutboundFilter:
    LIMIT = 2000

    @staticmethod
    def redact(text: str) -> str:
        value = str(text)
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub(
                lambda m: (
                    f"{m.group(1)}=[REDACTED]" if m.lastindex and m.lastindex >= 1 else "[REDACTED]"
                ),
                value,
            )
        value = _ACCOUNT_PATTERN.sub(lambda m: f"{m.group(1)}[REDACTED]{m.group(2)}", value)
        value = _COMMON_ACCOUNT_PATTERN.sub("[ACCOUNT REDACTED]", value)
        value = _QUOTED_PATH_PATTERN.sub("[LOCAL PATH REDACTED]", value)
        value = _PATH_PATTERN.sub("[LOCAL PATH REDACTED]", value)
        # Disable mass, role and user mentions without changing readable prose.
        value = _MENTION_PATTERN.sub(lambda m: m.group(0).replace("@", "@\u200b"), value)
        return value

    @classmethod
    def chunk(cls, text: str, citations: Sequence[Citation] = ()) -> list[str]:
        body = cls.redact(text).strip()
        # Full provenance is persisted with the turn. Keep the visible footer small.
        unique = list({(c.record_type, c.record_id): c for c in citations}.values())
        # Linked evidence in the answer doesn't need a second, identical source footer.
        unique = [c for c in unique if c.url and c.url.startswith("https://")
                  and c.url.split("?", 1)[0] not in body]
        # Internal references stay in the saved turn for follow-ups, not prose.
        cite_lines = [f"[{' '.join((c.label or 'Source').split())[:80]}](<{c.url}>)" for c in unique[:2]]
        suffix = cls.redact("\n" + " · ".join(cite_lines)) if cite_lines else ""
        combined = body + suffix
        if not combined:
            return [""]
        chunks: list[str] = []
        remaining = combined
        while len(remaining) > cls.LIMIT:
            boundary = remaining.rfind("\n", 0, cls.LIMIT + 1)
            if boundary < cls.LIMIT // 2:
                boundary = remaining.rfind(" ", 0, cls.LIMIT + 1)
            if boundary < cls.LIMIT // 2:
                boundary = cls.LIMIT
            piece = remaining[:boundary].rstrip()
            chunks.append(piece)
            remaining = remaining[boundary:].lstrip()
        if remaining or not chunks:
            chunks.append(remaining)
        return chunks


__all__ = ["OutboundFilter"]
