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

export function AdvisorWelcomeCard({ userId, gaps }: AdvisorWelcomeCardProps) {
  const [state, setState] = useState<WelcomeState>(INITIAL_STATE);

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
      setState({
        inFlightSynth: synth?.in_flight_synthesis ?? null,
        pendingDraft: draft,
        upcomingVests: (vestsOutlook?.upcoming ?? []).slice(0, 3),
        actionProposals: actions?.rows ?? [],
        anomalies: anoms ?? [],
        loading: false,
      });
    })();
    return () => {
      cancelled = true;
    };
  }, [userId]);

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
      </CardContent>
    </Card>
  );
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
