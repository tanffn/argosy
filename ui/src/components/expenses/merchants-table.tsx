"use client";

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";

import { HierarchicalCategoryPicker } from "./category-picker-hierarchical";
import {
  merchantsApi, type CategoryOut, type MerchantRow,
} from "@/lib/expenses/api";

interface Props {
  merchants: MerchantRow[];
  categories: CategoryOut[];
  userId: string;
  selected: Set<string>;
  onSelectionChange: (next: Set<string>) => void;
  onRowChanged: () => void;       // refetch
  onAddSubCategoryClick: () => void;
  busy: boolean;
}

function fmtNis(n: number): string {
  return n.toLocaleString("en-IL", {
    style: "currency", currency: "ILS", maximumFractionDigits: 0,
  });
}
function fmtUsd(n: number): string {
  return n.toLocaleString("en-US", {
    style: "currency", currency: "USD", maximumFractionDigits: 0,
  });
}

function SourceBadge({ source, isCached }: { source: string; isCached: boolean }) {
  if (!isCached) return <Badge variant="outline">uncached</Badge>;
  const variant: "default" | "secondary" | "outline" =
    source === "user" ? "default"
    : source === "llm" ? "secondary"
    : "outline";
  return <Badge variant={variant}>{source}</Badge>;
}

export function MerchantsTable({
  merchants, categories, userId, selected, onSelectionChange,
  onRowChanged, onAddSubCategoryClick, busy,
}: Props) {
  const [editingMerchant, setEditingMerchant] = useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [savingSlug, setSavingSlug] = useState<string | null>(null);

  function toggleRow(m: string) {
    const next = new Set(selected);
    if (next.has(m)) next.delete(m);
    else next.add(m);
    onSelectionChange(next);
  }

  function toggleAll() {
    if (selected.size === merchants.length) {
      onSelectionChange(new Set());
    } else {
      onSelectionChange(new Set(merchants.map((m) => m.merchant_normalized)));
    }
  }

  async function pickCategory(slug: string) {
    if (!editingMerchant) return;
    setSavingSlug(slug);
    try {
      await merchantsApi.patch(editingMerchant, {
        user_id: userId, category_slug: slug,
      });
      setPickerOpen(false);
      setEditingMerchant(null);
      onRowChanged();
    } catch (e) {
      alert(`Failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSavingSlug(null);
    }
  }

  async function confirmCurrent(merch: string) {
    try {
      await merchantsApi.patch(merch, { user_id: userId, confirm: true });
      onRowChanged();
    } catch (e) {
      alert(`Failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  return (
    <>
      <div className="overflow-x-auto border border-border rounded-md">
        <table className="min-w-full text-sm">
          <thead className="bg-muted/50">
            <tr>
              <th className="px-2 py-2 w-8">
                <Checkbox
                  checked={selected.size === merchants.length && merchants.length > 0}
                  onCheckedChange={toggleAll}
                />
              </th>
              <th className="px-2 py-2 text-left">Merchant</th>
              <th className="px-2 py-2 text-left">Category</th>
              <th className="px-2 py-2 text-right">Confidence</th>
              <th className="px-2 py-2 text-left">Source</th>
              <th className="px-2 py-2 text-right"># Txs</th>
              <th className="px-2 py-2 text-right">Total</th>
              <th className="px-2 py-2 text-right">Last seen</th>
              <th className="px-2 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {merchants.map((m) => (
              <tr key={m.merchant_normalized} className="border-t border-border">
                <td className="px-2 py-2">
                  <Checkbox
                    checked={selected.has(m.merchant_normalized)}
                    onCheckedChange={() => toggleRow(m.merchant_normalized)}
                  />
                </td>
                <td className="px-2 py-2 font-mono">{m.merchant_normalized}</td>
                <td className="px-2 py-2">
                  <Badge
                    variant="secondary"
                    className="cursor-pointer hover:bg-secondary/80"
                    onClick={() => {
                      setEditingMerchant(m.merchant_normalized);
                      setPickerOpen(true);
                    }}
                  >
                    {m.parent_label
                      ? `${m.parent_label} › ${m.category_label}`
                      : m.category_label}
                  </Badge>
                </td>
                <td className="px-2 py-2 text-right">
                  {m.confidence != null ? m.confidence.toFixed(2) : "—"}
                </td>
                <td className="px-2 py-2">
                  <SourceBadge source={m.source} isCached={m.is_cached} />
                </td>
                <td className="px-2 py-2 text-right">{m.tx_count}</td>
                <td className="px-2 py-2 text-right">
                  {m.total_nis ? fmtNis(m.total_nis) : ""}
                  {m.total_usd ? <div>{fmtUsd(m.total_usd)}</div> : null}
                </td>
                <td className="px-2 py-2 text-right text-xs text-muted-foreground">
                  {m.last_seen}
                </td>
                <td className="px-2 py-2 text-right">
                  {m.source !== "user" && m.is_cached && (
                    <Button
                      size="sm" variant="ghost"
                      disabled={busy}
                      onClick={() => confirmCurrent(m.merchant_normalized)}
                    >
                      Confirm
                    </Button>
                  )}
                </td>
              </tr>
            ))}
            {merchants.length === 0 && (
              <tr>
                <td colSpan={9} className="text-center text-muted-foreground py-8">
                  No merchants match the current filter.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <HierarchicalCategoryPicker
        open={pickerOpen}
        onOpenChange={(o) => { setPickerOpen(o); if (!o) setEditingMerchant(null); }}
        categories={categories}
        currentSlug={
          editingMerchant
            ? merchants.find((m) => m.merchant_normalized === editingMerchant)?.category_slug ?? null
            : null
        }
        onPick={pickCategory}
        onAddSubCategoryClick={onAddSubCategoryClick}
        busySlug={savingSlug}
      />
    </>
  );
}
