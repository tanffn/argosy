"use client";

import { CategoryEditPopover } from "@/components/expenses/category-edit-popover";
import { TagChip } from "@/components/expenses/tag-chip";
import { TagEditor } from "@/components/expenses/tag-editor";
import { Badge } from "@/components/ui/badge";
import {
  type CategoryOut,
  type SourceOut,
  type TransactionOut,
} from "@/lib/expenses/api";
import { formatCurrency, formatNIS } from "@/lib/expenses/format";

const USER_ID = "ariel";

interface TransactionsTableProps {
  transactions: TransactionOut[];
  categories: CategoryOut[];
  sources: SourceOut[];
  onCategoryChanged?: () => void;
  onTagsChanged?: () => void;
}

export function TransactionsTable({
  transactions, categories, sources, onCategoryChanged, onTagsChanged,
}: TransactionsTableProps) {
  const sourceById = new Map(sources.map((s) => [s.id, s]));

  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-xs text-muted-foreground border-b border-border">
          <th className="text-left py-2 pr-2">Date</th>
          <th className="text-left py-2 px-2">Merchant</th>
          <th className="text-left py-2 px-2">Category</th>
          <th className="text-left py-2 px-2">Tags</th>
          <th className="text-left py-2 px-2">Source</th>
          <th className="text-right py-2 pl-2">Amount</th>
        </tr>
      </thead>
      <tbody>
        {transactions.map((t) => {
          const src = sourceById.get(t.source_id);
          const isMoneyIn = t.direction === "credit" || t.tx_type === "refund";
          const amountText = t.amount_nis !== null
            ? formatNIS(t.amount_nis)
            : (t.amount_orig !== null && t.currency_orig !== null
              ? formatCurrency(t.amount_orig, t.currency_orig)
              : "—");
          const tags = t.tags ?? [];
          return (
            <tr key={t.id} className="border-b border-border/60 hover:bg-secondary/40">
              <td className="py-2 pr-2 tabular-nums whitespace-nowrap text-muted-foreground">
                {t.occurred_on}
              </td>
              <td className="py-2 px-2 truncate max-w-xs">{t.merchant_raw}</td>
              <td className="py-2 px-2">
                <CategoryEditPopover
                  txId={t.id}
                  userId={USER_ID}
                  currentSlug={t.category_slug}
                  categories={categories}
                  onChanged={() => onCategoryChanged?.()}
                />
              </td>
              <td className="py-2 px-2">
                <div className="flex flex-wrap items-center gap-1">
                  {tags.map((tag) => (
                    <TagChip key={tag} tag={tag} />
                  ))}
                  <TagEditor
                    txId={t.id}
                    userId={USER_ID}
                    currentTags={tags}
                    onChanged={() => onTagsChanged?.()}
                  />
                </div>
              </td>
              <td className="py-2 px-2 text-xs text-muted-foreground">
                {src?.display_name ?? `#${t.source_id}`}
              </td>
              <td
                className="py-2 pl-2 text-right tabular-nums whitespace-nowrap"
                title={isMoneyIn ? "Money in (credit)" : "Money out (debit)"}
              >
                <span className={isMoneyIn ? "text-emerald-600" : ""}>
                  {isMoneyIn ? `+${amountText}` : amountText}
                </span>
                {t.tx_type !== "regular" && (
                  <Badge variant="secondary" className="ml-2 text-xs">
                    {t.tx_type}
                  </Badge>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
