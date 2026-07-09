"use client";

/**
 * TradeRationale — structured rendering of a trade proposal's
 * ``rationale_summary``.
 *
 * The deep-decision trader emits (going forward) markdown sections —
 * ``**Verdict:** …`` / ``**Quality read:** …`` / ``**Price read:** …`` /
 * ``**Recommendation:** …`` — ending with a ``**Sources:**`` line. Older DB
 * rows carry the same section labels inline in ONE dense paragraph, with
 * bracket citations (``[fundamentals/NOW; debate: …, https://…]``) mid-
 * sentence. This component renders BOTH honestly:
 *
 *  1. Inline bracket citations are lifted out of the prose and collapsed
 *     into a trailing "Sources" line (URLs become links, never raw text).
 *  2. The text is split on the recognized section labels into short,
 *     labeled paragraphs.
 *  3. A rationale that matches NO known label still never renders as a
 *     single blob — it falls back to sentence-cluster paragraphs.
 */

import { useMemo } from "react";

export interface RationaleSection {
  label: string | null;
  text: string;
}

export interface ParsedRationale {
  sections: RationaleSection[];
  sources: string[];
}

// Section labels the trader emits (legacy inline and new markdown form).
// Matched case-insensitively but only accepted when the original text starts
// with a capital, so lowercase mid-sentence prose ("…our recommendation: …")
// never splits a section. Longest-first so "Quality read" wins over "Quality".
const SECTION_LABELS = [
  "Verdict",
  "Quality read",
  "Quality",
  "Price read",
  "Price",
  "Thesis-fit read",
  "Thesis fit read",
  "Thesis fit",
  "Thesis-fit",
  "Fit read",
  "Fit",
  "Data-quality caveats",
  "Data quality caveats",
  "Data quality",
  "Caveats",
  "Recommendation",
  "Estate note",
  "Estate",
  "Note",
  "Trigger to pause",
  "Trigger",
  "Sources",
];

// `**`-optional label, optional parenthetical ("Thesis-fit read (the
// decisive point)"), then `:` or an em/en dash separator.
const KNOWN_RE = new RegExp(
  `(?:\\*\\*)?(${SECTION_LABELS.join("|")})(\\s*\\([^)]{0,80}\\))?\\s*(?::\\s*(?:\\*\\*)?|(?:\\*\\*)?\\s*[—–]\\s*)`,
  "gi",
);

// Generic fallback heading: an ALL-CAPS phrase followed by a colon —
// the model improvises these ("TWO THESIS PROBLEMS SPECIFIC TO THIS BOOK:",
// "TRIGGER TO PAUSE:").
const CAPS_RE = /(?:\*\*)?([A-Z][A-Z](?:[A-Z -]{0,48}[A-Z])?)(\s*\([^)]{0,80}\))?\s*:\s*/g;

interface LabelMatch {
  index: number;
  length: number;
  label: string;
}

function findSectionMatches(text: string): LabelMatch[] {
  const found: LabelMatch[] = [];
  KNOWN_RE.lastIndex = 0;
  for (const m of text.matchAll(KNOWN_RE)) {
    // Reject lowercase mid-sentence hits ("…the verdict: …").
    if (!/[A-Z]/.test(m[1].charAt(0))) continue;
    found.push({ index: m.index ?? 0, length: m[0].length, label: m[1] });
  }
  CAPS_RE.lastIndex = 0;
  for (const m of text.matchAll(CAPS_RE)) {
    found.push({ index: m.index ?? 0, length: m[0].length, label: m[1] });
  }
  found.sort((a, b) => a.index - b.index || b.length - a.length);
  // Drop overlaps (a CAPS hit duplicating a known-label hit, or vice versa).
  const kept: LabelMatch[] = [];
  for (const f of found) {
    const prev = kept[kept.length - 1];
    if (prev && f.index < prev.index + prev.length) continue;
    kept.push(f);
  }
  return kept;
}

// A bracket / parenthesis group is lifted into Sources only when EVERY
// `;`/`,`-separated part is citation-shaped: a URL or a slash-path report id
// ("fundamentals/NOW", "domain_knowledge/tax/….md"), optionally prefixed
// with "debate:". The first path segment must contain a lowercase letter so
// financial abbreviations — "(D/E)", "(P/E)", "(EV/EBITDA)" — never read as
// citations. Ordinary parenthetical prose never matches.
const CITE_PART_RE =
  /^(?:debate\s*:\s*)?(?:https?:\/\/\S+|[\w-]*[a-z][\w-]*(?:\/[\w?=&.%-]+)+)$/;

function isCitationGroup(inner: string): boolean {
  const parts = inner
    .split(/[;,]/)
    .map((s) => s.trim())
    .filter(Boolean);
  return parts.length > 0 && parts.every((p) => CITE_PART_RE.test(p));
}

function collectSources(inner: string, into: string[]): void {
  for (const part of inner.split(/[;,]/)) {
    const s = part.replace(/^\s*debate\s*:\s*/i, "").trim();
    if (s && !into.includes(s)) into.push(s);
  }
}

/** Lift citation brackets/parens + bare URLs out of the prose into `sources`. */
function extractCitations(text: string): { cleaned: string; sources: string[] } {
  const sources: string[] = [];
  const lift = (whole: string, inner: string): string => {
    if (!isCitationGroup(inner)) return whole;
    collectSources(inner, sources);
    return "";
  };
  let cleaned = text
    .replace(/\[([^\][]+)\]/g, lift)
    .replace(/\(([^()]+)\)/g, lift);
  // Bare URLs left mid-sentence (outside brackets) also move to Sources.
  cleaned = cleaned.replace(/\(?\bhttps?:\/\/[^\s)\]]+\)?/g, (m) => {
    const url = m.replace(/^\(/, "").replace(/[).,;]+$/, "");
    if (!sources.includes(url)) sources.push(url);
    return "";
  });
  cleaned = cleaned
    .replace(/[ \t]+([.,;:!?])/g, "$1") // no space before punctuation
    .replace(/[ \t]{2,}/g, " ")
    .replace(/ +\n/g, "\n");
  return { cleaned, sources };
}

function normalizeLabel(raw: string): string {
  const flat = raw.trim().replace(/-/g, " ").replace(/\s+/g, " ");
  const lower = flat.toLowerCase();
  if (lower.startsWith("thesis fit") || lower === "fit read" || lower === "fit")
    return "Thesis fit";
  if (lower.startsWith("data quality") || lower === "caveats") return "Data quality";
  if (lower === "quality") return "Quality read";
  if (lower === "price") return "Price read";
  if (lower === "estate") return "Estate note";
  return lower.charAt(0).toUpperCase() + lower.slice(1);
}

function stripMd(s: string): string {
  return s.replace(/\*\*/g, "").trim();
}

/** Fallback: split unlabeled prose into ~2-sentence paragraphs. */
function sentenceClusters(text: string): RationaleSection[] {
  const out: RationaleSection[] = [];
  for (const para of text.split(/\n{2,}/)) {
    const p = para.trim();
    if (!p) continue;
    const sentences = p.split(/(?<=[.!?])\s+(?=["'(A-Z0-9])/);
    for (let i = 0; i < sentences.length; i += 2) {
      const chunk = sentences
        .slice(i, i + 2)
        .join(" ")
        .trim();
      if (chunk) out.push({ label: null, text: chunk });
    }
  }
  return out.length > 0 ? out : [{ label: null, text: text.trim() }];
}

export function parseRationale(raw: string): ParsedRationale {
  const { cleaned, sources } = extractCitations(raw ?? "");
  const text = cleaned.trim();
  if (!text) return { sections: [], sources };

  const matches = findSectionMatches(text);
  if (matches.length < 2) {
    // Not the structured pattern — never a single blob.
    return { sections: sentenceClusters(stripMd(text)), sources };
  }

  const sections: RationaleSection[] = [];
  const preamble = text.slice(0, matches[0].index).trim();
  if (stripMd(preamble)) sections.push({ label: null, text: stripMd(preamble) });
  for (let i = 0; i < matches.length; i++) {
    const m = matches[i];
    const start = m.index + m.length;
    const end = i + 1 < matches.length ? matches[i + 1].index : text.length;
    const body = stripMd(text.slice(start, end));
    const label = normalizeLabel(m.label);
    if (label === "Sources") {
      collectSources(body, sources);
      continue;
    }
    if (body) sections.push({ label, text: body });
  }
  return { sections, sources };
}

function SourceChip({ source }: { source: string }) {
  if (/^https?:\/\//.test(source)) {
    let host = source;
    try {
      host = new URL(source).hostname.replace(/^www\./, "");
    } catch {
      /* keep the raw string as the label */
    }
    return (
      <a
        href={source}
        target="_blank"
        rel="noreferrer"
        className="inline-block rounded-full border border-border/60 px-2 py-0.5 text-[11px] text-primary hover:underline"
      >
        {host}
      </a>
    );
  }
  return (
    <span className="inline-block rounded-full border border-border/60 px-2 py-0.5 text-[11px] text-muted-foreground">
      {source}
    </span>
  );
}

export function TradeRationale({ text }: { text: string }) {
  const parsed = useMemo(() => parseRationale(text), [text]);
  if (parsed.sections.length === 0 && parsed.sources.length === 0) return null;
  return (
    <div className="space-y-2">
      {parsed.sections.map((s, i) => (
        <p key={i} className="text-sm leading-relaxed">
          {s.label && <span className="font-semibold">{s.label}: </span>}
          {s.text}
        </p>
      ))}
      {parsed.sources.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 pt-1">
          <span className="text-[11px] font-medium text-muted-foreground">Sources</span>
          {parsed.sources.map((src) => (
            <SourceChip key={src} source={src} />
          ))}
        </div>
      )}
    </div>
  );
}
