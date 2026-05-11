"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";

import { AddSubCategoryDialog } from "@/components/expenses/add-subcategory-dialog";
import { HierarchicalCategoryPicker } from "@/components/expenses/category-picker-hierarchical";
import { MerchantsTable } from "@/components/expenses/merchants-table";
import {
  expensesApi, merchantsApi,
  type CategoryOut, type MerchantRow,
} from "@/lib/expenses/api";

const USER_ID = "ariel";

export default function MerchantsPage() {
  const [merchants, setMerchants] = useState<MerchantRow[]>([]);
  const [categories, setCategories] = useState<CategoryOut[]>([]);
  const [search, setSearch] = useState("");
  const [sourceFilter, setSourceFilter] = useState<string>("all");
  const [categoryFilter, setCategoryFilter] = useState<string>("all");
  const [maxConfidence, setMaxConfidence] = useState<string>("");
  const [sort, setSort] = useState<string>("needs_attention");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [addSubCatOpen, setAddSubCatOpen] = useState(false);
  const [bulkPickerOpen, setBulkPickerOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const fetchAll = useCallback(async () => {
    const cats = await expensesApi.categories(USER_ID);
    setCategories(cats.categories);
    const ms = await merchantsApi.list({
      user_id: USER_ID,
      search: search || undefined,
      source: sourceFilter === "all" ? undefined : sourceFilter,
      category: categoryFilter === "all" ? undefined : categoryFilter,
      max_confidence: maxConfidence ? Number(maxConfidence) : undefined,
      sort,
      order,
      limit: 500,
    });
    setMerchants(ms.merchants);
  }, [search, sourceFilter, categoryFilter, maxConfidence, sort, order]);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  async function applyBulkCategory(slug: string) {
    setBusy(true);
    try {
      const res = await merchantsApi.bulkCategory({
        user_id: USER_ID,
        merchant_normalizeds: Array.from(selected),
        category_slug: slug,
      });
      alert(`Applied to ${res.ok_count} merchants (${res.total_affected_transactions} transactions). ${res.error_count} failed.`);
      setBulkPickerOpen(false);
      setSelected(new Set());
      await fetchAll();
    } finally {
      setBusy(false);
    }
  }

  async function confirmBulk() {
    setBusy(true);
    try {
      const res = await merchantsApi.bulkCategory({
        user_id: USER_ID,
        merchant_normalizeds: Array.from(selected),
        confirm: true,
      });
      alert(`Confirmed ${res.ok_count} merchants. ${res.error_count} failed.`);
      setSelected(new Set());
      await fetchAll();
    } finally {
      setBusy(false);
    }
  }

  const affectedTxCount = useMemo(() => {
    return merchants
      .filter((m) => selected.has(m.merchant_normalized))
      .reduce((s, m) => s + m.tx_count, 0);
  }, [selected, merchants]);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Input
          placeholder="Search merchant…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-56"
        />
        <Select value={categoryFilter} onValueChange={setCategoryFilter}>
          <SelectTrigger className="w-44"><SelectValue placeholder="Category" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All categories</SelectItem>
            <SelectItem value="uncategorized">Uncategorized</SelectItem>
            {categories
              .filter((c) => !c.parent_slug && c.slug !== "uncategorized")
              .map((c) => (
                <SelectItem key={c.slug} value={c.slug}>{c.label_en}</SelectItem>
              ))}
          </SelectContent>
        </Select>
        <Select value={sourceFilter} onValueChange={setSourceFilter}>
          <SelectTrigger className="w-32"><SelectValue placeholder="Source" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All sources</SelectItem>
            <SelectItem value="user">User-confirmed</SelectItem>
            <SelectItem value="llm">LLM cached</SelectItem>
            <SelectItem value="uncached">Uncached</SelectItem>
          </SelectContent>
        </Select>
        <Input
          type="number"
          step="0.01"
          min="0"
          max="1"
          placeholder="Max confidence"
          value={maxConfidence}
          onChange={(e) => setMaxConfidence(e.target.value)}
          className="w-32"
        />
        <Select value={sort} onValueChange={setSort}>
          <SelectTrigger className="w-44"><SelectValue placeholder="Sort by" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="needs_attention">Needs attention</SelectItem>
            <SelectItem value="merchant">Merchant</SelectItem>
            <SelectItem value="category">Category</SelectItem>
            <SelectItem value="confidence">Confidence</SelectItem>
            <SelectItem value="tx_count"># Txs</SelectItem>
            <SelectItem value="total_nis">Total</SelectItem>
            <SelectItem value="last_seen">Last seen</SelectItem>
          </SelectContent>
        </Select>
        <Button
          variant="outline"
          onClick={() => setOrder((o) => (o === "asc" ? "desc" : "asc"))}
        >
          {order === "asc" ? "▲" : "▼"}
        </Button>
      </div>

      <MerchantsTable
        merchants={merchants}
        categories={categories}
        userId={USER_ID}
        selected={selected}
        onSelectionChange={setSelected}
        onRowChanged={fetchAll}
        onAddSubCategoryClick={() => setAddSubCatOpen(true)}
        busy={busy}
        sort={sort}
        order={order}
        onSortChange={(s, o) => { setSort(s); setOrder(o); }}
      />

      {selected.size > 0 && (
        <div className="sticky bottom-2 bg-background border border-border rounded-md p-3 shadow flex items-center gap-3">
          <span className="text-sm">
            {selected.size} merchants selected · {affectedTxCount} transactions
          </span>
          <Button onClick={() => setBulkPickerOpen(true)} disabled={busy}>
            Apply category…
          </Button>
          <Button variant="outline" onClick={confirmBulk} disabled={busy}>
            Confirm current
          </Button>
          <Button variant="ghost" onClick={() => setSelected(new Set())}>
            Clear
          </Button>
        </div>
      )}

      <AddSubCategoryDialog
        open={addSubCatOpen}
        onOpenChange={setAddSubCatOpen}
        userId={USER_ID}
        categories={categories}
        onCreated={() => fetchAll()}
      />

      <HierarchicalCategoryPicker
        open={bulkPickerOpen}
        onOpenChange={setBulkPickerOpen}
        categories={categories}
        currentSlug={null}
        onPick={applyBulkCategory}
        onAddSubCategoryClick={() => { setBulkPickerOpen(false); setAddSubCatOpen(true); }}
      />
    </div>
  );
}
