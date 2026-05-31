"use client";

/**
 * Server-aggregated welcome card on /advisor. Fans out to existing
 * REST routes on mount (no LLM, no agent fleet) so the page is
 * useful within the first ~500 ms instead of waiting on a full
 * advisor turn before showing anything.
 *
 * Sections render conditionally — anything with zero items is
 * hidden. When everything is quiet, the card collapses to a
 * greeting + a hint to use the chat or the gap tracker.
 */

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  api,
  type ActionProposalDTO,
  type AdvisorGapsResponse,
  type AnomalyCardDTO,
  type DraftResponse,
  type InFlightSynthesisDTO,
  type UpcomingVestDTO,
} from "@/lib/api";

interface AdvisorWelcomeCardProps {
  userId: string;
  gaps: AdvisorGapsResponse | null;
}

interface WelcomeState {
  inFlightSynth: InFlightSynthesisDTO | null;
  pendingDraft: DraftResponse | null;
  upcomingVests: UpcomingVestDTO[];
  actionProposals: ActionProposalDTO[];
  anomalies: AnomalyCardDTO[];
  loading: boolean;
}

const INITIAL_STATE: WelcomeState = {
  inFlightSynth: null,
  pendingDraft: null,
  upcomingVests: [],
  actionProposals: [],
  anomalies: [],
  loading: true,
};

interface InsightState {
  status: "idle" | "loading" | "done" | "failed";
  text: string;
}

export function AdvisorWelcomeCard({ userId, gaps }: AdvisorWelcomeCardProps) {
  const [state, setState] = useState<WelcomeState>(INITIAL_STATE);
  const [insight, setInsight] = useState<InsightState>({
    status: "idle",
    text: "",
  });

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount fan-out, see Markdown / Plan pages for the canonical pattern.
    void (async () => {
      const [synth, draft, vestsOutlook, actions, anoms] = await Promise.all([
        api
          .planInFlightSynthesis(userId)
          .catch(() => ({ in_flight_synthesis: null })),
        api.planDraft(userId).catch(() => null),
        api.upcomingVests(userId, 90).catch(() => null),
        api
          .getActionProposals({ userId, status: "pending" })
          .catch(() => null),
        api.anomalyHighlights(userId, 3).catch(() => []),
      ]);
      if (cancelled) return;
      const next: WelcomeState = {
        inFlightSynth: synth?.in_flight_synthesis ?? null,
        pendingDraft: draft,
        upcomingVests: (vestsOutlook?.upcoming ?? []).slice(0, 3),
        actionProposals: actions?.rows ?? [],
        anomalies: anoms ?? [],
        loading: false,
      };
      setState(next);

      // Kick off the LLM hydration call once the static surface is
      // rendered. We pass the same summary the user can already see on
      // the static card, plus the gap-tracker headline, so the agent
      // has the full picture without re-fetching server-side.
      setInsight({ status: "loading", text: "" });
      const summary = buildStateSummary(next, gaps);
      try {
        const r = await api.advisorInsight(userId, summary);
        if (cancelled) return;
        const t = (r.insight || "").trim();
        setInsight({ status: t ? "done" : "idle", text: t });
      } catch {
        if (cancelled) return;
        // The route degrades to insight="" on its own errors; reaching
        // here means a real network failure. Hide the section.
        setInsight({ status: "failed", text: "" });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [userId, gaps]);

  if (state.loading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Welcome back</CardTitle>
          <CardDescription>Pulling today&apos;s context…</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const inProgress: ReactNode[] = [];
  if (state.inFlightSynth) {
    const s = state.inFlightSynth;
    inProgress.push(
      <li key="synth">
        Plan synthesis #{s.decision_run_id} in flight — phase {s.completed_phases} of {s.total_phases}.{" "}
        <Link href="/plan" className="text-info hover:underline">
          /plan →
        </Link>
      </li>,
    );
  }
  if (state.pendingDraft) {
    inProgress.push(
      <li key="draft">
        Plan draft #{state.pendingDraft.plan_version_id} pending review.{" "}
        <Link href="/plan" className="text-info hover:underline">
          /plan →
        </Link>
      </li>,
    );
  }

  const comingUp: ReactNode[] = [];
  for (const v of state.upcomingVests) {
    comingUp.push(
      <li key={`vest-${v.grant_id}-${v.expected_vest_date}`}>
        RSU vest in {v.days_until} day{v.days_until === 1 ? "" : "s"} —
        grant {v.grant_id} · {v.shares_projected} NVDA shares ({v.expected_vest_date}).{" "}
        <Link href="/retirement#upcoming-vests" className="text-info hover:underline">
          /retirement →
        </Link>
      </li>,
    );
  }

  const attention: ReactNode[] = [];
  for (const p of state.actionProposals.slice(0, 3)) {
    attention.push(
      <li key={`prop-${p.id}`}>
        <span className="font-mono text-[11px] text-muted-foreground mr-1.5">
          [{p.severity}]
        </span>
        {p.summary}{" "}
        <Link
          href={`/proposals#action-${p.id}`}
          className="text-info hover:underline"
        >
          review →
        </Link>
      </li>,
    );
  }
  for (const a of state.anomalies.slice(0, 3)) {
    attention.push(
      <li key={`anom-${a.id}`}>
        <span className="font-mono text-[11px] text-muted-foreground mr-1.5">
          [{a.severity}]
        </span>
        {a.message}
        {a.link ? (
          <>
            {" "}
            <Link href={a.link} className="text-info hover:underline">
              open →
            </Link>
          </>
        ) : null}
      </li>,
    );
  }

  const gapsHint =
    gaps && (gaps.counts.missing > 0 || gaps.counts.stale > 0) ? (
      <p className="text-xs text-muted-foreground">
        Open context gaps:{" "}
        <strong className="text-warning">{gaps.counts.stale}</strong> stale ·{" "}
        <strong className="text-error">{gaps.counts.missing}</strong> missing —
        click a row in the tracker on the right to fill one.
      </p>
    ) : null;

  const allQuiet =
    inProgress.length === 0 && comingUp.length === 0 && attention.length === 0;

  if (allQuiet && !gapsHint) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Welcome back</CardTitle>
          <CardDescription>
            Nothing urgent today. Ask me anything below.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Welcome back</CardTitle>
        <CardDescription>
          Here&apos;s what&apos;s worth knowing right now.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {inProgress.length > 0 && (
          <WelcomeSection title="In progress">{inProgress}</WelcomeSection>
        )}
        {comingUp.length > 0 && (
          <WelcomeSection title="Coming up">{comingUp}</WelcomeSection>
        )}
        {attention.length > 0 && (
          <WelcomeSection title="Needs your attention">{attention}</WelcomeSection>
        )}
        {gapsHint}
        <InsightSlot state={insight} />
      </CardContent>
    </Card>
  );
}

function InsightSlot({ state }: { state: InsightState }) {
  if (state.status === "loading") {
    return (
      <section className="text-xs text-muted-foreground italic">
        Looking for today&apos;s most useful insight…
      </section>
    );
  }
  if (state.status === "done" && state.text) {
    return (
      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-1.5">
          Today&apos;s insight
        </h3>
        <p className="text-sm">{state.text}</p>
      </section>
    );
  }
  return null;
}

function WelcomeSection({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <section>
      <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-1.5">
        {title}
      </h3>
      <ul className="list-disc pl-5 text-sm flex flex-col gap-1.5">
        {children}
      </ul>
    </section>
  );
}

/**
 * Assemble a compact markdown summary of the current state for the
 * insight agent. Mirrors what the static card already shows the user
 * — no extra signals; the LLM should base its judgment on the same
 * surface the user is looking at, not a richer hidden view.
 */
function buildStateSummary(
  state: WelcomeState,
  gaps: AdvisorGapsResponse | null,
): string {
  const lines: string[] = [];

  lines.push("## In progress");
  if (state.inFlightSynth) {
    const s = state.inFlightSynth;
    lines.push(
      `- plan synthesis #${s.decision_run_id} running, phase ${s.completed_phases}/${s.total_phases}`,
    );
  }
  if (state.pendingDraft) {
    lines.push(
      `- plan draft #${state.pendingDraft.plan_version_id} pending review`,
    );
  }
  if (lines[lines.length - 1] === "## In progress") {
    lines.push("- (nothing in flight)");
  }

  lines.push("");
  lines.push("## Coming up");
  if (state.upcomingVests.length === 0) {
    lines.push("- (no RSU vests within 90 days)");
  } else {
    for (const v of state.upcomingVests) {
      lines.push(
        `- RSU vest in ${v.days_until}d (grant ${v.grant_id}, ${v.shares_projected} NVDA shares, ${v.expected_vest_date})`,
      );
    }
  }

  lines.push("");
  lines.push("## Needs your attention");
  if (state.actionProposals.length === 0 && state.anomalies.length === 0) {
    lines.push("- (no pending action proposals or anomalies)");
  } else {
    for (const p of state.actionProposals.slice(0, 3)) {
      lines.push(`- [proposal/${p.severity}] ${p.summary}`);
    }
    for (const a of state.anomalies.slice(0, 3)) {
      lines.push(`- [anomaly/${a.severity}] ${a.message}`);
    }
  }

  if (gaps) {
    lines.push("");
    lines.push("## Context gap tracker");
    lines.push(
      `- counts: fresh=${gaps.counts.fresh} stale=${gaps.counts.stale} missing=${gaps.counts.missing}`,
    );
    const open = gaps.items.filter(
      (it) => it.state === "missing" || it.state === "stale",
    );
    for (const it of open.slice(0, 4)) {
      lines.push(`- [${it.state}] ${it.label}`);
    }
  }

  return lines.join("\n");
}
