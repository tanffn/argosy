"use client";

import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { CollapsibleSection } from "@/components/ui/collapsible-section";
import { expensesApi, type TagRuleOut } from "@/lib/expenses/api";

/** Collapsed-by-default list of brush-paint tag rules with delete. */
export function TagRulesPanel({
  userId,
  refreshKey = 0,
}: {
  userId: string;
  refreshKey?: number;
}) {
  const [rules, setRules] = useState<TagRuleOut[]>([]);
  const [busyId, setBusyId] = useState<number | null>(null);

  const refresh = useCallback(() => {
    expensesApi
      .listTagRules(userId)
      .then((r) => setRules(r.rules))
      .catch(() => setRules([]));
  }, [userId]);

  useEffect(() => {
    refresh();
  }, [refresh, refreshKey]);

  async function remove(id: number) {
    setBusyId(id);
    try {
      await expensesApi.deleteTagRule(id, userId);
      refresh();
    } catch (e) {
      alert(`Failed: ${e}`);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <CollapsibleSection
      title="Tag rules (always-tag)"
      summary={
        rules.length === 0
          ? "none"
          : `${rules.length} rule${rules.length === 1 ? "" : "s"}`
      }
    >
      {rules.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          No brush rules yet. When tagging a merchant, check &quot;Always tag
          this merchant&quot; to create one.
        </p>
      ) : (
        <ul className="space-y-2 text-xs">
          {rules.map((r) => (
            <li
              key={r.id}
              className="flex flex-wrap items-center justify-between gap-2 border-b border-border/40 pb-2"
            >
              <div className="min-w-0 font-mono">
                <span className="text-foreground">{r.match_merchant_normalized}</span>
                {r.match_category_slug && (
                  <span className="text-muted-foreground">
                    {" "}
                    · {r.match_category_slug}
                  </span>
                )}
                <span className="text-muted-foreground"> → </span>
                <span className="font-semibold">{r.tag}</span>
              </div>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-7 text-xs"
                disabled={busyId === r.id}
                onClick={() => void remove(r.id)}
              >
                Delete
              </Button>
            </li>
          ))}
        </ul>
      )}
    </CollapsibleSection>
  );
}
