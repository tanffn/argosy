"use client";

import type { WholePortfolioComparisonDTO } from "@/lib/api";

function dollars(value: string | null | undefined) {
  return value != null && Number.isFinite(Number(value))
    ? Number(value).toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 0 })
    : "Unavailable";
}

function percent(value: string | null | undefined) {
  return value != null && Number.isFinite(Number(value)) ? `${Number(value).toFixed(2)}%` : "Not yet measurable";
}

export function WholePortfolioComparison({ data }: { data: WholePortfolioComparisonDTO }) {
  const names = { unchanged: "Keep recorded portfolio", argosy: "Follow Argosy (paper)", spy: "S&P 500 proxy (SPY)" };
  const latest = data.latest;
  const books = latest?.books;
  return (
    <section className="rounded border border-border/60 p-3 text-xs" aria-label="Whole portfolio comparison">
      <h3 className="font-medium">Did Argosy improve the whole portfolio?</h3>
      {data.status === "not_started" ? (
        <p className="mt-1 text-muted-foreground">The scheduled evaluator will freeze a recorded-portfolio baseline. No comparison has been started yet.</p>
      ) : (
        <>
          <p className="mt-1 text-muted-foreground">
            Recorded liquid portfolio · {dollars(data.recorded_liquid_usd)} at activation · {data.position_count} positions, including unmanaged holdings.
            {" "}This is a forward paper comparison, not your actual trading return.
          </p>
          {!books && <p className="mt-2">{latest?.status === "baseline_incomplete"
            ? "Baseline awaiting complete price/FX evidence. Missing holdings have not been dropped."
            : `Waiting for the first completed market session after activation${data.scheduled_baseline_date ? ` (${data.scheduled_baseline_date})` : ""}. No gains or losses are claimed yet.`}</p>}
          {data.status === "partial" && <p className="mt-2">Comparison incomplete: some historical marks or recommendations could not be replayed. No whole-portfolio outperformance is claimed.</p>}
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-left">
              <thead><tr><th className="py-1 pr-3">Path</th><th className="pr-3">Marked value</th><th className="pr-3">Gross return</th><th>After modeled tax/costs</th></tr></thead>
              <tbody>
                {(Object.keys(names) as Array<keyof typeof names>).map((name) => {
                  const gross = books?.gross?.[name];
                  const net = books?.net?.[name];
                  return <tr key={name} className="border-t border-border/30">
                    <td className="py-1 pr-3">{names[name]}</td>
                    <td className="pr-3">{dollars(gross?.nav_usd)}</td>
                    <td className="pr-3">{percent(gross?.return_pct)}</td>
                    <td>{net?.after_tax_nav_usd != null ? percent(net.return_pct) : "Unknown / pending evidence"}</td>
                  </tr>;
                })}
              </tbody>
            </table>
          </div>
          <details className="mt-2 text-muted-foreground">
            <summary className="cursor-pointer">Accounting, coverage and recommendation details</summary>
            <p className="mt-1">Baseline snapshot #{data.snapshot_id}; {data.baseline_date ?? "first completed session pending"}. Version: {data.version}.</p>
            <p>Native cash: {(data.cash_accounts ?? []).map((row) => `${row.account}: ${row.quantity ?? "unknown"} ${row.currency}`).join(" · ")}</p>
            <p>Fresh imports do not rebase this comparison. Deposits are not gains. Repeated recommendations are not repeat buys.</p>
            <p>After-tax values deduct only modeled realized taxes and costs, not hypothetical liquidation tax. Unknown fees or basis are not assumed to be zero.</p>
            <p>{data.policy?.income_coverage}</p>
            {(data.excluded ?? []).map((row, index) => <p key={index}>Outside scope: {row.name ?? "Physical property"} — {row.reason}.</p>)}
            {(data.warnings?.length ?? 0) > 0 && <p>{data.warnings!.length} holdings retain older custody observation dates. The baseline is as recorded, not a new broker statement.</p>}
            {latest?.baseline?.missing?.length ? <p>Unpriced baseline components: {latest.baseline.missing.join(", ")}</p> : null}
            {books && (Object.keys(names) as Array<keyof typeof names>).map((name) => <div key={name} className="mt-1">
              <span className="font-medium">{names[name]}: </span>
              cash {dollars(books.gross[name]?.cash_usd)};
              {" "}{books.gross[name]?.missing.length ?? 0} missing marks;
              {" "}{books.gross[name]?.stale_marks.length ?? 0} carried marks;
              {" "}modeled fees {books.net[name]?.after_tax_nav_usd != null ? dollars(books.net[name]?.fees_usd) : "unresolved"},
              {" "}tax {books.net[name]?.after_tax_nav_usd != null ? dollars(books.net[name]?.taxes_usd) : "unresolved"}.
              {(books.net[name]?.unknowns ?? []).map((problem, index) => <p key={index}>{problem}</p>)}
            </div>)}
            <ul className="mt-2 space-y-1">
              {(data.applications ?? []).slice(-12).map((row, index) => <li key={index}>
                {row.date} · {row.mode ?? "receipt"} · {row.status.replaceAll("_", " ")}
                {row.failures?.length ? `: ${row.failures.join("; ")}` : ""}
              </li>)}
            </ul>
            <p className="mt-1">Daily time-weighted returns; dated paper prices. Dividend receivables are not spendable until payment is evidenced. None of this places orders or proves investment skill.</p>
          </details>
        </>
      )}
    </section>
  );
}
