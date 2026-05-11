"use client";

import { useState } from "react";

import { AddSubCategoryDialog } from "@/components/expenses/add-subcategory-dialog";
import { LabelEditor } from "@/components/expenses/label-editor";
import { TagChip } from "@/components/expenses/tag-chip";
import { TagEditor } from "@/components/expenses/tag-editor";
import { TransactionDetailsDialog } from "@/components/expenses/transaction-details-dialog";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import {
  expensesApi,
  transactionsApi,
  type CategoryOut,
  type SourceOut,
  type TransactionOut,
} from "@/lib/expenses/api";
import { useFxMode } from "@/lib/expenses/fx-mode";
import { formatCurrency, formatNIS } from "@/lib/expenses/format";

const USER_ID = "ariel";

interface TransactionsTableProps {
  transactions: TransactionOut[];
  categories: CategoryOut[];
  sources: SourceOut[];
  onCategoryChanged?: () => void;
  onTagsChanged?: () => void;
  selected?: Set<number>;
  onSelectionChange?: (next: Set<number>) => void;
}

export function TransactionsTable({
  transactions, categories, sources, onCategoryChanged, onTagsChanged,
  selected, onSelectionChange,
}: TransactionsTableProps) {
  const sourceById = new Map(sources.map((s) => [s.id, s]));
  const [editingTx, setEditingTx] = useState<{ id: number; slug: string | null; tags: string[]; merchant_normalized: string } | null>(null);
  const [addSubCatOpen, setAddSubCatOpen] = useState(false);
  const [detailsTx, setDetailsTx] = useState<TransactionOut | null>(null);
  const [fxMode] = useFxMode();

  return (
    <>
    <table className="w-full text-sm">
      <thead>
        <tr className="text-xs text-muted-foreground border-b border-border">
          {onSelectionChange && (
            <th className="px-2 py-2 w-8">
              <Checkbox
                checked={selected?.size === transactions.length && transactions.length > 0}
                onCheckedChange={() => {
                  if (!onSelectionChange) return;
                  if (selected?.size === transactions.length) onSelectionChange(new Set());
                  else onSelectionChange(new Set(transactions.map((t) => t.id)));
                }}
              />
            </th>
          )}
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
          const amountText =
            fxMode === "nis" && t.amount_nis_converted !== null
              ? formatNIS(t.amount_nis_converted)
              : t.amount_nis !== null
                ? formatNIS(t.amount_nis)
                : (t.amount_orig !== null && t.currency_orig !== null
                  ? formatCurrency(t.amount_orig, t.currency_orig)
                  : "—");
          const tags = t.tags ?? [];
          return (
            <tr key={t.id} className="border-b border-border/60 hover:bg-secondary/40">
              {onSelectionChange && (
                <td className="px-2 py-2">
                  <Checkbox
                    checked={selected?.has(t.id) ?? false}
                    onCheckedChange={() => {
                      if (!onSelectionChange || !selected) return;
                      const next = new Set(selected);
                      if (next.has(t.id)) next.delete(t.id); else next.add(t.id);
                      onSelectionChange(next);
                    }}
                  />
                </td>
              )}
              <td className="py-2 pr-2 tabular-nums whitespace-nowrap text-muted-foreground">
                {t.occurred_on}
              </td>
              <td className="py-2 px-2 max-w-xs">
                <div className="flex items-center gap-1">
                  <span className="truncate">{t.merchant_raw}</span>
                  <button
                    type="button"
                    onClick={() => setDetailsTx(t)}
                    className="text-muted-foreground hover:text-foreground text-xs shrink-0"
                    aria-label="Show transaction details"
                    title="Show original row + open source file"
                  >
                    ⓘ
                  </button>
                </div>
              </td>
              <td className="py-2 px-2">
                <Badge
                  variant="secondary"
                  className="cursor-pointer hover:bg-secondary/80 capitalize"
                  onClick={() => setEditingTx({
                    id: t.id,
                    slug: t.category_slug ?? null,
                    tags: t.tags ?? [],
                    merchant_normalized: t.merchant_normalized,
                  })}
                >
                  {t.category_slug ?? "uncategorized"}
                </Badge>
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
                <a
                  href={`/expenses/sources#source-${t.source_id}`}
                  className="hover:underline hover:text-foreground"
                  title={
                    src
                      ? `${src.display_name} — ${src.issuer} ${src.external_id} (${src.kind}). Click to inspect the source's statements.`
                      : `Source #${t.source_id}`
                  }
                >
                  {src?.display_name ?? `#${t.source_id}`}
                </a>
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
    {editingTx && (
      <LabelEditor
        open
        onOpenChange={(o) => { if (!o) setEditingTx(null); }}
        mode="single-tx"
        categories={categories}
        currentSlug={editingTx.slug}
        currentTags={editingTx.tags}
        showSiblingsCheckbox={true}
        onAddSubCategoryClick={() => setAddSubCatOpen(true)}
        onSubmit={async ({ categorySlug, addTags, removeTags, applyToSiblings }) => {
          if (categorySlug) {
            await expensesApi.patchTransactionCategory(
              editingTx.id, USER_ID, categorySlug, applyToSiblings,
            );
          }
          if (addTags.length || removeTags.length) {
            // When "Apply to all siblings" is checked, tags fan out to every
            // transaction sharing this merchant_normalized — matching the
            // category fan-out behaviour. Otherwise just the editing tx.
            let txIds: number[] = [editingTx.id];
            if (applyToSiblings) {
              const sibs = await expensesApi.transactions(USER_ID, {
                merchant_normalized: editingTx.merchant_normalized,
                limit: 10000,
              });
              txIds = sibs.transactions.map((s) => s.id);
            }
            await transactionsApi.bulkLabel({
              user_id: USER_ID,
              transaction_ids: txIds,
              add_tags: addTags,
              remove_tags: removeTags,
            });
          }
          setEditingTx(null);
          onCategoryChanged?.();
        }}
      />
    )}
    <AddSubCategoryDialog
      open={addSubCatOpen}
      onOpenChange={setAddSubCatOpen}
      userId={USER_ID}
      categories={categories}
      onCreated={() => onCategoryChanged?.()}
    />
    {detailsTx && (
      <TransactionDetailsDialog
        tx={detailsTx}
        open
        onOpenChange={(o) => { if (!o) setDetailsTx(null); }}
      />
    )}
    </>
  );
}
