"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { TransactionsTable } from "@/components/expenses/transactions-table";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { LabelEditor } from "@/components/expenses/label-editor";
import {
  expensesApi,
  transactionsApi,
  type CategoryOut,
  type SourceOut,
  type TransactionsResponse,
} from "@/lib/expenses/api";

const USER_ID = "ariel";
const PAGE_SIZE = 100;

function TransactionsPageInner() {
  const router = useRouter();
  const params = useSearchParams();
  const [data, setData] = useState<TransactionsResponse | null>(null);
  const [categories, setCategories] = useState<CategoryOut[]>([]);
  const [sources, setSources] = useState<SourceOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [labelEditorOpen, setLabelEditorOpen] = useState(false);

  const filterParams = {
    category: params.get("category") ?? undefined,
    source_id: params.get("source_id") ? Number(params.get("source_id")) : undefined,
    direction: params.get("direction") as "debit" | "credit" | undefined,
    search: params.get("search") ?? undefined,
    from_date: params.get("from_date") ?? undefined,
    to_date: params.get("to_date") ?? undefined,
    include_card_payments: params.get("include_card_payments") === "1",
    tag: params.get("tag") ?? undefined,
  };

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [tx, cats, srcs] = await Promise.all([
        expensesApi.transactions(USER_ID, {
          ...filterParams,
          limit: PAGE_SIZE,
          offset: page * PAGE_SIZE,
        }),
        expensesApi.categories(USER_ID),
        expensesApi.sources(USER_ID),
      ]);
      setData(tx);
      setCategories(cats.categories);
      setSources(srcs.sources);
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(filterParams), page]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  function setParam(key: string, value: string | null) {
    const next = new URLSearchParams(params.toString());
    if (value === null || value === "") next.delete(key);
    else next.set(key, value);
    router.replace(`/expenses/transactions?${next.toString()}`);
    setPage(0);
  }

  const total = data?.total ?? 0;
  const showing = data?.transactions.length ?? 0;

  return (
    <div className="flex flex-col gap-3">
      <Card>
        <CardContent className="p-4 flex flex-wrap gap-2 items-end">
          <Input
            placeholder="Search merchant…"
            defaultValue={filterParams.search ?? ""}
            onBlur={(e) => setParam("search", e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") setParam("search", e.currentTarget.value);
            }}
            className="max-w-xs"
          />
          <select
            value={filterParams.category ?? ""}
            onChange={(e) => setParam("category", e.target.value || null)}
            className="bg-background border border-border rounded px-2 py-1.5 text-sm"
          >
            <option value="">All categories</option>
            {categories.map((c) => (
              <option key={c.slug} value={c.slug}>{c.label_en}</option>
            ))}
          </select>
          <select
            value={filterParams.source_id ?? ""}
            onChange={(e) => setParam("source_id", e.target.value || null)}
            className="bg-background border border-border rounded px-2 py-1.5 text-sm"
          >
            <option value="">All sources</option>
            {sources.map((s) => (
              <option key={s.id} value={s.id}>{s.display_name}</option>
            ))}
          </select>
          <select
            value={filterParams.direction ?? ""}
            onChange={(e) => setParam("direction", e.target.value || null)}
            className="bg-background border border-border rounded px-2 py-1.5 text-sm"
            title="Spending = money out (debit). Money in = income or refunds (credit)."
          >
            <option value="">All</option>
            <option value="debit">Spending (out)</option>
            <option value="credit">Money in (income / refunds)</option>
          </select>
          <Input
            placeholder="Tag (e.g. trip:greece-2026-aug)"
            defaultValue={filterParams.tag ?? ""}
            onBlur={(e) => setParam("tag", e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") setParam("tag", e.currentTarget.value);
            }}
            className="max-w-xs"
          />
          <label
            className="flex items-center gap-1 text-xs text-muted-foreground"
            title="Filter to transactions on/after this date"
          >
            From
            <Input
              type="date"
              defaultValue={filterParams.from_date ?? ""}
              onChange={(e) => setParam("from_date", e.target.value || null)}
              className="w-36"
            />
          </label>
          <label
            className="flex items-center gap-1 text-xs text-muted-foreground"
            title="Filter to transactions on/before this date"
          >
            To
            <Input
              type="date"
              defaultValue={filterParams.to_date ?? ""}
              onChange={(e) => setParam("to_date", e.target.value || null)}
              className="w-36"
            />
          </label>
          <details className="basis-full mt-1">
            <summary className="text-xs text-muted-foreground cursor-pointer select-none">
              Advanced
            </summary>
            <div className="mt-2 pl-3 border-l border-border">
              <label
                className="text-xs text-muted-foreground inline-flex items-center gap-2"
                title="Bank rows that pay off a card statement — usually filtered to avoid double-counting."
              >
                <input
                  type="checkbox"
                  checked={filterParams.include_card_payments}
                  onChange={(e) =>
                    setParam("include_card_payments", e.target.checked ? "1" : null)
                  }
                />
                <span>
                  Show card-payment settlements
                  <span className="ml-1 text-muted-foreground/80">
                    (bank rows that pay off a card statement — hidden by default to
                    avoid double-counting)
                  </span>
                </span>
              </label>
            </div>
          </details>
        </CardContent>
      </Card>
      {selected.size === 0 && total > showing && (
        <button
          type="button"
          onClick={async () => {
            const ids: number[] = [];
            let off = 0;
            const PAGE = 1000;
            while (true) {
              const res = await expensesApi.transactions(USER_ID, {
                ...filterParams,
                limit: PAGE,
                offset: off,
              });
              for (const tx of res.transactions) ids.push(tx.id);
              if (res.transactions.length < PAGE) break;
              off += PAGE;
            }
            setSelected(new Set(ids));
          }}
          className="text-xs underline text-muted-foreground"
        >
          Select all matching filter ({total} transactions)
        </button>
      )}
      <Card>
        <CardContent className="p-4 overflow-x-auto">
          {loading && !data ? (
            <div className="text-sm text-muted-foreground py-6 text-center">
              Loading transactions…
            </div>
          ) : (
            <>
              <div className="text-xs text-muted-foreground mb-2">
                {showing} of {total} transactions
              </div>
              <TransactionsTable
                transactions={data?.transactions ?? []}
                categories={categories}
                sources={sources}
                onCategoryChanged={refresh}
                onTagsChanged={refresh}
                selected={selected}
                onSelectionChange={setSelected}
              />
              <div className="flex items-center justify-between mt-3 text-sm">
                <Button
                  variant="outline" size="sm"
                  disabled={page === 0}
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                >
                  ← Prev
                </Button>
                <span className="text-muted-foreground text-xs">
                  Page {page + 1} of {Math.max(1, Math.ceil(total / PAGE_SIZE))}
                </span>
                <Button
                  variant="outline" size="sm"
                  disabled={(page + 1) * PAGE_SIZE >= total}
                  onClick={() => setPage((p) => p + 1)}
                >
                  Next →
                </Button>
              </div>
            </>
          )}
        </CardContent>
      </Card>
      {selected.size > 0 && (
        <>
          <div className="sticky bottom-2 bg-background border border-border rounded-md p-3 shadow flex items-center gap-3">
            <span className="text-sm">{selected.size} transactions selected</span>
            <Button onClick={() => setLabelEditorOpen(true)}>Apply labels…</Button>
            <Button variant="ghost" onClick={() => setSelected(new Set())}>Clear</Button>
          </div>
          <LabelEditor
            open={labelEditorOpen}
            onOpenChange={setLabelEditorOpen}
            mode="bulk-tx"
            categories={categories}
            currentSlug={null}
            currentTags={[]}
            showSiblingsCheckbox={false}
            bulkCount={selected.size}
            onSubmit={async ({ categorySlug, addTags, removeTags }) => {
              const res = await transactionsApi.bulkLabel({
                user_id: USER_ID,
                transaction_ids: Array.from(selected),
                category_slug: categorySlug,
                add_tags: addTags,
                remove_tags: removeTags,
              });
              alert(`Updated ${res.affected} transactions. ${res.skipped.length} skipped.`);
              setSelected(new Set());
              await refresh();
            }}
          />
        </>
      )}
    </div>
  );
}

export default function TransactionsPage() {
  return (
    <Suspense fallback={<div className="text-sm text-muted-foreground py-6 text-center">Loading…</div>}>
      <TransactionsPageInner />
    </Suspense>
  );
}
