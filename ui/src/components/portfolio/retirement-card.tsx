"use client";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { formatNis, formatPct, formatUsd } from "@/components/portfolio/stat-card";
import { WealthTrajectoryChart } from "@/components/portfolio/wealth-trajectory-chart";
import type { WealthAssumptions, WealthRetirementBlock } from "@/lib/api";
import { cn } from "@/lib/utils";

interface RetirementCardProps {
  retirement: WealthRetirementBlock;
  assumptions: WealthAssumptions;
}

const SCENARIO_LABELS: Record<string, { display: string; tone: string }> = {
  bear: { display: "Bear", tone: "text-error" },
  conservative: { display: "Conservative", tone: "text-info" },
  typical: { display: "Typical", tone: "text-success" },
};

/**
 * Row 1 of the Wealth Dashboard — the full-width financial-independence
 * headline.
 *
 * Top stripe: net worth + monthly burn / income / surplus.
 * Middle: 25-year wealth-trajectory chart, 3 lines + FIRE-target reference.
 * Bottom: 3 scenario tiles (bear / conservative / typical) with their
 *   projected target retirement age + FIRE target.
 * Footer: assumptions block citing SWR, FX source, current_age inferred
 *   vs known, plan target source, scenario real_return values.
 */
export function RetirementCard({
  retirement,
  assumptions,
}: RetirementCardProps) {
  const surplusPct =
    retirement.monthly_income_nis && retirement.monthly_income_nis > 0
      ? ((retirement.monthly_surplus_nis ?? 0) / retirement.monthly_income_nis) * 100
      : null;

  return (
    <Card className="w-full">
      <CardHeader>
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div>
            <CardTitle className="text-lg">Financial independence</CardTitle>
            <CardDescription>
              Three return scenarios, 25-year horizon, target = annual expenses ÷
              SWR
            </CardDescription>
          </div>
          <div className="text-right">
            <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground">
              SWR
            </div>
            <div className="font-mono text-base">
              {(assumptions.swr_rate * 100).toFixed(1)}%
            </div>
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        {/* Top stripe — headline numbers */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Stat label="Net worth (NIS)" value={formatNis(retirement.net_worth_nis)} sub={retirement.net_worth_usd != null ? formatUsd(retirement.net_worth_usd) : null} />
          <Stat label="Monthly burn" value={`${formatNis(retirement.monthly_burn_nis)} NIS`} />
          <Stat label="Monthly income" value={`${formatNis(retirement.monthly_income_nis)} NIS`} />
          <Stat
            label="Monthly surplus"
            value={`${formatNis(retirement.monthly_surplus_nis)} NIS`}
            sub={surplusPct != null ? `${formatPct(surplusPct, 0)} of income` : null}
            tone={
              retirement.monthly_surplus_nis == null
                ? "muted"
                : retirement.monthly_surplus_nis > 0
                  ? "success"
                  : "error"
            }
          />
        </div>

        {/* Wealth trajectory chart */}
        <div>
          <WealthTrajectoryChart retirement={retirement} />
        </div>

        {/* Scenario cards */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {retirement.scenarios.map((s) => (
            <ScenarioTile
              key={s.name}
              name={s.name}
              realReturn={s.real_return}
              yearsToTarget={s.years_to_target}
              targetAge={s.target_age}
              targetPortfolioNis={s.target_portfolio_nis}
              currentAge={retirement.current_age}
            />
          ))}
        </div>

        {/* Assumptions footer */}
        <div className="border-t border-border/40 pt-3">
          <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground mb-1.5">
            Assumptions
          </div>
          <ul className="text-xs text-muted-foreground flex flex-wrap gap-x-4 gap-y-1">
            <li>SWR <span className="font-mono">{(assumptions.swr_rate * 100).toFixed(1)}%</span> (per plan)</li>
            <li>
              Real returns —{" "}
              {Object.entries(assumptions.scenario_returns)
                .map(([k, v]) => `${k} ${(v * 100).toFixed(1)}%`)
                .join(", ")}
            </li>
            <li>
              Current age{" "}
              <span className="font-mono">{assumptions.current_age}</span>{" "}
              <span
                className={cn(
                  "italic",
                  retirement.current_age_inferred && "text-warning",
                )}
              >
                ({assumptions.current_age_source})
              </span>
            </li>
            <li>
              FX USD/NIS{" "}
              <span className="font-mono">
                {assumptions.fx_usd_nis?.toFixed(4) ?? "—"}
              </span>{" "}
              <span className="italic">({assumptions.fx_source})</span>
            </li>
            <li>
              Plan target source —{" "}
              <span className="italic">
                {assumptions.nvda_target_source ?? "no plan target found"}
              </span>
            </li>
            {assumptions.snapshot_date && (
              <li>
                Snapshot <span className="font-mono">{assumptions.snapshot_date}</span>
              </li>
            )}
          </ul>
          {retirement.missing_reasons.length > 0 && (
            <ul className="mt-2 text-xs text-warning flex flex-col gap-0.5">
              {retirement.missing_reasons.map((r, i) => (
                <li key={i}>• {r}</li>
              ))}
            </ul>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

/** Top-stripe headline stat (small label + big number + optional sub). */
function Stat({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  tone?: "success" | "error" | "muted";
}) {
  const toneClass =
    tone === "success"
      ? "text-success"
      : tone === "error"
        ? "text-error"
        : tone === "muted"
          ? "text-muted-foreground"
          : "text-foreground";
  return (
    <div className="flex flex-col gap-0.5">
      <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div className={cn("text-lg font-mono font-semibold", toneClass)}>
        {value}
      </div>
      {sub && <div className="text-xs text-muted-foreground">{sub}</div>}
    </div>
  );
}

/** Scenario card — three columns at the bottom of the retirement block. */
function ScenarioTile({
  name,
  realReturn,
  yearsToTarget,
  targetAge,
  targetPortfolioNis,
  currentAge,
}: {
  name: string;
  realReturn: number;
  yearsToTarget: number | null;
  targetAge: number | null;
  targetPortfolioNis: number | null;
  currentAge: number;
}) {
  const meta = SCENARIO_LABELS[name] ?? { display: name, tone: "text-foreground" };
  const alreadyThere = yearsToTarget === 0;
  const unreachable = yearsToTarget === null;

  return (
    <Card className="py-3">
      <CardContent className="px-4 flex flex-col gap-1.5">
        <div className="flex items-baseline justify-between">
          <div className={cn("text-sm font-medium", meta.tone)}>{meta.display}</div>
          <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground">
            real {(realReturn * 100).toFixed(1)}%
          </div>
        </div>
        {alreadyThere ? (
          <div className="text-base font-mono text-success">
            Already at target
          </div>
        ) : unreachable ? (
          <div className="text-base font-mono text-error">Not reachable</div>
        ) : (
          <div className="text-base font-mono">
            age {targetAge ?? "—"}{" "}
            <span className="text-xs text-muted-foreground">
              ({(yearsToTarget ?? 0).toFixed(1)} yrs)
            </span>
          </div>
        )}
        <div className="text-xs text-muted-foreground">
          target {formatNis(targetPortfolioNis)} NIS
          {!alreadyThere && !unreachable && (
            <>
              <span className="mx-1">·</span>
              from age {currentAge}
            </>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
