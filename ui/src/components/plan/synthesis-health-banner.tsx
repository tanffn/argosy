"use client";

import Link from "next/link";
import { AlertTriangle, CheckCircle2 } from "lucide-react";

import { Banner } from "@/components/ui/banner";
import type { SynthesisHealth } from "@/lib/api";

interface SynthesisHealthBannerProps {
  health: SynthesisHealth | null | undefined;
  decisionRunId: number | null | undefined;
}

/**
 * One-row "is this draft built on a sound fleet run?" chip (T0.7).
 *
 * Renders above the FMObjectionsCard on /plan regardless of whether the
 * Fund Manager approved or rejected — the point is to give the user
 * positive confirmation when "all 18 agents OK / all 8 adapters OK" so
 * they don't have to dig into /decisions/{id} to see that the agents
 * actually ran. Green tone when nothing failed, warning tone otherwise.
 *
 * Hidden when ``health`` is null / undefined or when ``decisionRunId``
 * is missing (legacy drafts, or drafts whose decision_run_id pointed at
 * a non-existent run — the backend deliberately returns null in that
 * case so the banner just doesn't render).
 */
export function SynthesisHealthBanner({
  health,
  decisionRunId,
}: SynthesisHealthBannerProps) {
  if (!health || decisionRunId == null) return null;

  const anyFailure = health.agents_failed > 0 || health.adapters_failed > 0;
  const tone = anyFailure ? "warning" : "success";
  const Icon = anyFailure ? AlertTriangle : CheckCircle2;
  const drillHref = `/decisions/${decisionRunId}`;

  // "skipped" (agent didn't run at all — e.g. codex zigzag not triggered)
  // is shown as a separate count so it doesn't masquerade as a failure.
  // Older backends without this field (cached responses) get `?? 0` so
  // the line still renders.
  const agentsSkipped = health.agents_skipped ?? 0;

  // "unavailable" adapters (auth/tier-blocked sources, Cloudflare
  // challenges, instruments a source structurally doesn't cover) are a
  // known, non-actionable gap — shown separately so they don't read as
  // failures. `?? 0` for older backends that don't send the field.
  const adaptersUnavailable = health.adapters_unavailable ?? 0;

  return (
    <Banner
      tone={tone}
      icon={<Icon className="h-4 w-4" />}
      title="Synthesis fleet health"
      data-testid="synthesis-health-banner"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs font-mono">
          {health.agents_ok} agents OK · {health.agents_failed} failed
          {agentsSkipped > 0 ? ` · ${agentsSkipped} skipped` : ""}
          {" · "}
          {health.adapters_ok} adapters OK · {health.adapters_failed} adapter
          failures
          {adaptersUnavailable > 0
            ? ` · ${adaptersUnavailable} unavailable (tier/coverage)`
            : ""}
        </span>
        <Link
          href={drillHref}
          className="text-xs font-medium underline-offset-4 hover:underline text-card-foreground"
        >
          Drill in →
        </Link>
      </div>
    </Banner>
  );
}
