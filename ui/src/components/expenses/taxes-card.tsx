"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { type TaxesSummary } from "@/lib/expenses/api";
import { formatNIS } from "@/lib/expenses/format";

const USD_FMT = new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", maximumFractionDigits: 0,
});

const KIND_LABELS: Record<string, string> = {
  income_tax_paid: "Income tax",
  social_security_paid: "Social security",
  property_tax: "Property tax (Arnona)",
  other_taxes: "Other taxes",
  rsu_withholding_usd: "RSU withholding (USD)",
};

interface TaxesCardProps {
  data: TaxesSummary;
}

function MiniBars({ values, height = 28 }: { values: number[]; height?: number }) {
  const max = Math.max(1, ...values);
  return (
    <svg width={values.length * 6} height={height} className="block">
      {values.map((v, i) => {
        const h = Math.max(1, Math.round((v / max) * (height - 4)));
        return (
          <rect
            key={i}
            x={i * 6}
            y={height - h - 2}
            width={4}
            height={h}
            fill="currentColor"
            className="text-amber-700"
          />
        );
      })}
    </svg>
  );
}

export function TaxesCard({ data }: TaxesCardProps) {
  const byKind = data.by_kind ?? {};
  const kinds = Object.entries(byKind).filter(([, v]) => v > 0);
  const trend = (data.trend_12mo ?? []).map((p) => p.total_nis);
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">
          Taxes paid
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="flex items-baseline justify-between gap-3">
          <div>
            <div className="text-2xl font-semibold">
              {formatNIS(data.yearly_total_nis ?? 0)}
            </div>
            <div className="text-xs text-muted-foreground">
              last 12mo · NIS direct (income+property+SS)
            </div>
          </div>
          {data.yearly_total_usd > 0 && (
            <div className="text-right">
              <div className="text-base font-medium">
                {USD_FMT.format(data.yearly_total_usd)}
              </div>
              <div className="text-xs text-muted-foreground">
                RSU withholding (Schwab)
              </div>
            </div>
          )}
        </div>
        {trend.length > 0 && (
          <div className="mt-2">
            <MiniBars values={trend} />
          </div>
        )}
        {kinds.length > 0 && (
          <details className="mt-3 group">
            <summary className="text-xs text-muted-foreground cursor-pointer select-none hover:text-foreground">
              Show by kind
            </summary>
            <div className="mt-2 grid grid-cols-1 gap-1 text-xs">
              {kinds.map(([k, v]) => (
                <div key={k} className="flex justify-between border-b border-border/40 py-1">
                  <span className="text-muted-foreground">
                    {KIND_LABELS[k] ?? k}
                  </span>
                  <span className="tabular-nums">
                    {k.endsWith("_usd")
                      ? USD_FMT.format(v)
                      : formatNIS(v)}
                  </span>
                </div>
              ))}
            </div>
          </details>
        )}
      </CardContent>
    </Card>
  );
}
