"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { TransactionsTable } from "@/components/expenses/transactions-table";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  expensesApi,
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

  const filterParams = {
    category: params.get("category") ?? undefined,
    source_id: params.get("source_id") ? Number(params.get("source_id")) : undefined,
    direction: params.get("direction") as "debit" | "credit" | undefined,
    search: params.get("search") ?? undefined,
    from_date: params.get("from_date") ?? undefined,
    to_date: params.get("to_date") ?? undefined,
    include_card_payments: params.get("include_card_payments") === "1",
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
          >
            <option value="">Both</option>
            <option value="debit">Debits</option>
            <option value="credit">Credits</option>
          </select>
          <label className="text-xs text-muted-foreground inline-flex items-center gap-1">
            <input
              type="checkbox"
              checked={filterParams.include_card_payments}
              onChange={(e) => setParam("include_card_payments", e.target.checked ? "1" : null)}
            />
            include card-payments
          </label>
        </CardContent>
      </Card>
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
