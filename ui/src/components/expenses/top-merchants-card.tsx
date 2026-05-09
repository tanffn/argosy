"use client";

import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { type MerchantSpend } from "@/lib/expenses/api";
import { formatNIS } from "@/lib/expenses/format";

interface TopMerchantsCardProps {
  data: MerchantSpend[];
}

export function TopMerchantsCard({ data }: TopMerchantsCardProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Top merchants — current month</CardTitle>
      </CardHeader>
      <CardContent>
        {data.length === 0 ? (
          <div className="text-sm text-muted-foreground py-6 text-center">
            No merchants yet.
          </div>
        ) : (
          <ol className="divide-y divide-border">
            {data.map((m, i) => (
              <li key={m.merchant_normalized} className="flex items-center gap-3 py-2">
                <span className="text-xs text-muted-foreground tabular-nums w-5">
                  {i + 1}.
                </span>
                <Link
                  href={`/expenses/transactions?search=${encodeURIComponent(m.merchant_display)}`}
                  className="flex-1 min-w-0 truncate hover:underline"
                  title={m.merchant_display}
                >
                  {m.merchant_display}
                </Link>
                {m.category_slug && (
                  <Badge variant="secondary" className="text-xs capitalize">
                    {m.category_slug.replace(/_/g, " ")}
                  </Badge>
                )}
                <span className="text-sm tabular-nums text-right w-20">
                  {formatNIS(m.total_nis)}
                </span>
                <span className="text-xs text-muted-foreground tabular-nums w-8 text-right">
                  ×{m.transaction_count}
                </span>
              </li>
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}
