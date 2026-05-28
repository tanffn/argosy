"use client";

import { useEffect, useState } from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { MethodologyPanel } from "@/components/retirement/MethodologyPanel";
import { api, type WithdrawalPoliciesResponse, type WithdrawalPolicy } from "@/lib/api";

interface Props {
  /** Initial policy selection. */
  initialPolicyId?: WithdrawalPolicy["id"];
  /** Optional callback when user picks a different policy. */
  onChange?: (policyId: WithdrawalPolicy["id"]) => void;
}

/**
 * WithdrawalPolicySelector — pick the spend-down rule for retirement.
 *
 * Shows four policies (Bengen 4% / Guyton-Klinger / VPW / Bucket) with
 * one-paragraph rationale each. The selected policy will (in a follow-up
 * commit) drive the per-month withdrawal in the Monte Carlo path; for
 * now this is the user-facing surface to pick + understand.
 */
export function WithdrawalPolicySelector({
  initialPolicyId = "guyton_klinger",
  onChange,
}: Props) {
  const [data, setData] = useState<WithdrawalPoliciesResponse | null>(null);
  const [selected, setSelected] = useState<WithdrawalPolicy["id"]>(initialPolicyId);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.retirement
      .withdrawalPolicies()
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (err) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Withdrawal policy</CardTitle>
          <CardDescription className="text-rose-400">Failed: {err}</CardDescription>
        </CardHeader>
      </Card>
    );
  }
  if (!data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Withdrawal policy</CardTitle>
          <CardDescription>Loading…</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const selectedPolicy = data.policies.find((p) => p.id === selected);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Withdrawal policy</CardTitle>
        <CardDescription>
          How to spend the portfolio without going broke. Each policy has
          different trade-offs around income smoothness vs. ruin risk.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="space-y-2">
          {data.policies.map((p) => (
            <label
              key={p.id}
              className={`flex items-start gap-3 rounded-md border px-3 py-2 cursor-pointer transition-colors ${
                selected === p.id
                  ? "border-foreground/40 bg-background/60"
                  : "border-border/40 hover:border-border/70"
              }`}
            >
              <input
                type="radio"
                name="withdrawal_policy"
                value={p.id}
                checked={selected === p.id}
                onChange={() => {
                  setSelected(p.id);
                  onChange?.(p.id);
                }}
                className="mt-1"
              />
              <span>
                <span className="block font-medium text-sm">{p.label}</span>
                <span className="block text-xs text-muted-foreground mt-0.5">
                  {p.rationale}
                </span>
                <span className="block text-[10px] font-mono text-muted-foreground mt-1">
                  source: <a href={`#src-${p.source_id}`} className="hover:underline">{p.source_id}</a>
                </span>
              </span>
            </label>
          ))}
        </div>

        {selectedPolicy && (selected === "vpw" || selected === "bucket") && (
          <div className="mt-3 rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-muted-foreground">
            <span className="font-medium text-amber-400">Caveat:</span>{" "}
            {selected === "vpw" ? (
              <>
                VPW eliminates literal zero-balance ruin by construction
                (you always have something left to spend), but the hero
                verdict can still show OFF_TRACK because P(solvent) here
                means &quot;P(covering essential expenses at age 95)&quot; — VPW's
                age-banded draws may fall well below your expense need
                during stressed periods.
              </>
            ) : (
              <>
                Bucket caps draws to ~5% of remaining portfolio when
                stressed. Like VPW, this trades hitting zero for under-
                spending in down years. Hero verdict reflects expense-
                coverage, not literal ruin.
              </>
            )}
          </div>
        )}

        <DrilldownSection title="Methodology">
          <MethodologyPanel>
            <p>
              Four shipped policies, each implementing a different theory
              of how to draw a sustainable income from a finite portfolio:
            </p>
            <ul className="list-disc pl-5">
              <li>
                <b>Bengen 4%</b>: Fixed-real withdrawal at 4% initial WR.
                Simple, well-known, but vulnerable to early sequence shocks.
              </li>
              <li>
                <b>Guyton-Klinger</b>: 5% initial with guardrails — cut 10%
                when overdrawing, ratchet up 10% in good years. Argosy
                default; empirically resilient for concentrated portfolios.
              </li>
              <li>
                <b>VPW</b>: Spend a fraction of current balance, age-banded.
                Zero ruin risk by construction; income fluctuates with markets.
              </li>
              <li>
                <b>Bucket</b>: Behaves like Bengen but caps draw at the cash-
                bucket equivalent (~2y essential expenses) when stressed.
                Trades efficiency for behavioral resilience.
              </li>
            </ul>
            <p>
              Currently selected: <b>{selectedPolicy?.label}</b>. In a
              follow-up commit this selection drives the Monte Carlo
              path's per-month withdrawal.
            </p>
          </MethodologyPanel>
        </DrilldownSection>
      </CardContent>
    </Card>
  );
}
