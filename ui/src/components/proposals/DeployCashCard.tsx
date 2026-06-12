"use client";
import type { DeploymentPlanDTO, DeploymentTierDTO } from "@/lib/api";

const TIER_LABEL: Record<string, string> = {
  reserve: "Reserve",
  core: "Core",
  medium: "Medium",
  high: "High",
};

function TierHeading({ tier }: { tier: DeploymentTierDTO }) {
  return (
    <div className="font-semibold">
      <span>{TIER_LABEL[tier.name]}</span>
      {` ($${tier.total_usd.toLocaleString()})`}
    </div>
  );
}

function TierBlock({ tier }: { tier: DeploymentTierDTO }) {
  if (tier.lines.length === 0) {
    return (
      <div className="mt-3">
        <TierHeading tier={tier} />
        <div className="text-sm text-muted-foreground">
          {tier.name === "core" ? "—" : "Populated in a later phase."}
        </div>
      </div>
    );
  }
  return (
    <div className="mt-3">
      <TierHeading tier={tier} />
      <table className="w-full text-sm">
        <thead>
          <tr>
            <th>SYMBOL</th>
            <th>TYPE</th>
            <th>AMOUNT</th>
            <th>TIMING</th>
            <th>NEW?</th>
            <th>ESTATE</th>
            <th>REASON</th>
          </tr>
        </thead>
        <tbody>
          {tier.lines.map((l) => (
            <tr key={`${tier.name}-${l.symbol}`}>
              <td>{l.symbol}</td>
              <td>{l.type}</td>
              <td>{`$${l.amount_usd.toLocaleString()}`}</td>
              <td>{l.timing}</td>
              <td>{l.is_new ? "NEW" : "held"}</td>
              <td>{l.estate.status.replace(/_/g, " ")}</td>
              <td>
                {l.cap_note}
                {l.rationale ? ` — ${l.rationale}` : ""}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function DeployCashCard({
  plan,
  loading,
  amount,
  onAmountChange,
  unallocatedUsd,
}: {
  plan: DeploymentPlanDTO | null;
  loading: boolean;
  amount: number;
  onAmountChange: (v: number) => void;
  unallocatedUsd: number;
}) {
  return (
    <section className="rounded-lg border p-4">
      <h2 className="text-lg font-semibold">Deploy Cash</h2>
      <div className="text-sm text-muted-foreground">
        {`Unallocated cash: $${unallocatedUsd.toLocaleString()}`}
      </div>
      <label className="mt-2 block text-sm">
        Amount to deploy (net of tax)
        <input
          type="number"
          value={amount}
          onChange={(e) => onAmountChange(Number(e.target.value))}
          className="ml-2 rounded border px-2 py-1"
        />
      </label>
      {loading && <div className="mt-3 text-sm">Computing…</div>}
      {!loading && plan && (
        <>
          {plan.note && (
            <div className="mt-2 text-sm italic">{plan.note}</div>
          )}
          <div className="mt-2 text-sm">
            <span>{`Deployed: $${plan.deployed_total_usd.toLocaleString()}`}</span>
            {plan.undeployed_remainder_usd > 0 && (
              <span className="ml-3 text-amber-600">
                {`Undeployed remainder: $${plan.undeployed_remainder_usd.toLocaleString()}`}
              </span>
            )}
          </div>
          <div className="text-xs text-muted-foreground">
            {`US-situs estate exposure (planned buys): $${plan.us_situs_exposed_usd.toLocaleString()}`}
            {plan.us_situs_sanctioned_usd > 0 &&
              ` · sanctioned NVDA sleeve: $${plan.us_situs_sanctioned_usd.toLocaleString()}`}
          </div>
          {plan.tiers.map((t) => (
            <TierBlock key={t.name} tier={t} />
          ))}
          <ul className="mt-3 list-disc pl-5 text-xs text-muted-foreground">
            {plan.caveats.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
