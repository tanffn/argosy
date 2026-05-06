"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ChevronDown,
  ChevronUp,
  FileText,
  Pencil,
  RefreshCw,
} from "lucide-react";

import { Markdown } from "@/components/markdown";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, type BaselineResponse } from "@/lib/api";
import { DistillateEditDialog } from "./distillate-edit-dialog";

interface PlanInScopeCardProps {
  userId: string;
}

type EditTarget = {
  category: "goals" | "principles" | "decision_rules" | "targets" | "constraints";
  itemLabel: string;
  value: string;
  fieldLabel: string;
  fieldKey: "value" | "detail" | "rule" | "rationale";
};

/**
 * Plan-in-scope card — renders the imported baseline distillate at the
 * top of the Advisor page. See SDD §6.10.
 *
 * States:
 *  - loading  → skeleton header
 *  - error    → red description
 *  - no data  → soft empty state (no plan imported yet)
 *  - data     → full distillate view with per-item edit buttons
 */
export function PlanInScopeCard({ userId }: PlanInScopeCardProps) {
  const [data, setData] = useState<BaselineResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(true);
  const [redistilling, setRedistilling] = useState(false);
  const [editTarget, setEditTarget] = useState<EditTarget | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await api.planBaseline(userId);
      setData(r);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      if (msg.includes("404")) {
        // No baseline yet — soft empty state, not an error.
        setData(null);
      } else {
        setError(msg);
      }
    } finally {
      setLoading(false);
    }
  }, [userId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const onRedistill = async () => {
    setRedistilling(true);
    try {
      const r = await api.planBaselineDistill(userId, true);
      setData(r);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRedistilling(false);
    }
  };

  // ---- Loading state -------------------------------------------------------
  if (loading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Plan in scope</CardTitle>
          <CardDescription>Loading...</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  // ---- Error state ---------------------------------------------------------
  if (error) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Plan in scope</CardTitle>
          <CardDescription className="text-red-500">{error}</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  // ---- Empty state (no plan imported yet) ----------------------------------
  if (!data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <FileText className="h-4 w-4" /> No plan imported yet
          </CardTitle>
          <CardDescription>
            Upload a Markdown plan below and the advisor will distill the
            durable principles before our first conversation.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  // ---- Populated card ------------------------------------------------------
  const distilledAt = data.distilled_at
    ? new Date(data.distilled_at).toLocaleString()
    : "(not yet distilled)";

  const openEdit = (t: EditTarget) => setEditTarget(t);

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <div>
          <CardTitle className="text-base flex items-center gap-2">
            <FileText className="h-4 w-4" />
            Plan in scope: {data.version_label || "(untitled)"}
          </CardTitle>
          <CardDescription>
            Baseline · distilled {distilledAt}
            {!data.distillate && " · awaiting distillation"}
          </CardDescription>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={onRedistill}
            disabled={redistilling}
            title="Re-run the distiller against the imported plan"
          >
            <RefreshCw
              className={`h-3 w-3 mr-1 ${redistilling ? "animate-spin" : ""}`}
            />
            Re-distill
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setExpanded((v) => !v)}
            aria-label={expanded ? "Collapse" : "Expand"}
          >
            {expanded ? (
              <ChevronUp className="h-4 w-4" />
            ) : (
              <ChevronDown className="h-4 w-4" />
            )}
          </Button>
        </div>
      </CardHeader>

      {/* Structured distillate view */}
      {expanded && data.distillate && (
        <CardContent className="flex flex-col gap-4">
          {data.distillate.goals.length > 0 && (
            <Section
              title="Goals"
              items={data.distillate.goals.map((g) => ({
                key: g.label,
                left: <strong>{g.label}</strong>,
                right: g.value,
                edited: g.user_edited,
                onEdit: () =>
                  openEdit({
                    category: "goals",
                    itemLabel: g.label,
                    value: g.value,
                    fieldKey: "value",
                    fieldLabel: "Value",
                  }),
              }))}
            />
          )}
          {data.distillate.principles.length > 0 && (
            <Section
              title="Principles"
              items={data.distillate.principles.map((p) => ({
                key: p.label,
                left: <strong>{p.label}</strong>,
                right: p.rationale,
                edited: p.user_edited,
                onEdit: () =>
                  openEdit({
                    category: "principles",
                    itemLabel: p.label,
                    value: p.rationale,
                    fieldKey: "rationale",
                    fieldLabel: "Rationale",
                  }),
              }))}
            />
          )}
          {data.distillate.targets.length > 0 && (
            <Section
              title="Targets (working assumptions, not eternal)"
              items={data.distillate.targets.map((t) => ({
                key: t.label,
                left: <strong>{t.label}</strong>,
                right: `${t.value} ${t.unit} (stated ${t.stated_at}; revisit ${t.revisit_after})`,
                edited: t.user_edited,
                onEdit: () =>
                  openEdit({
                    category: "targets",
                    itemLabel: t.label,
                    value: String(t.value),
                    fieldKey: "value",
                    fieldLabel: "Value",
                  }),
              }))}
            />
          )}
          {data.distillate.decision_rules.length > 0 && (
            <Section
              title="Decision rules"
              items={data.distillate.decision_rules.map((r) => ({
                key: r.label,
                left: <strong>{r.label}</strong>,
                right: r.rule,
                edited: r.user_edited,
                onEdit: () =>
                  openEdit({
                    category: "decision_rules",
                    itemLabel: r.label,
                    value: r.rule,
                    fieldKey: "rule",
                    fieldLabel: "Rule",
                  }),
              }))}
            />
          )}
          {data.distillate.constraints.length > 0 && (
            <Section
              title="Constraints"
              items={data.distillate.constraints.map((c) => ({
                key: c.label,
                left: <strong>{c.label}</strong>,
                right: c.detail,
                edited: c.user_edited,
                onEdit: () =>
                  openEdit({
                    category: "constraints",
                    itemLabel: c.label,
                    value: c.detail,
                    fieldKey: "detail",
                    fieldLabel: "Detail",
                  }),
              }))}
            />
          )}
          {data.distillate.risk_priorities.length > 0 && (
            <div>
              <p className="text-sm font-semibold uppercase tracking-wide text-muted-foreground mb-1">
                Risk priorities (ordered)
              </p>
              <ol className="list-decimal list-inside text-sm">
                {data.distillate.risk_priorities.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ol>
            </div>
          )}
          {data.distillate.stress_tolerance && (
            <div>
              <p className="text-sm font-semibold uppercase tracking-wide text-muted-foreground mb-1">
                Stress tolerance
              </p>
              <p className="text-sm">{data.distillate.stress_tolerance}</p>
            </div>
          )}
        </CardContent>
      )}

      {/* Fallback: rendered markdown when no structured distillate yet */}
      {expanded && !data.distillate && data.distillate_rendered && (
        <CardContent>
          <Markdown>{data.distillate_rendered}</Markdown>
        </CardContent>
      )}

      {/* Per-item edit dialog */}
      {editTarget && (
        <DistillateEditDialog
          open={true}
          onOpenChange={(v) => !v && setEditTarget(null)}
          userId={userId}
          category={editTarget.category}
          itemLabel={editTarget.itemLabel}
          initialValue={editTarget.value}
          fieldLabel={editTarget.fieldLabel}
          fieldKey={editTarget.fieldKey}
          onSaved={(next) => setData(next)}
        />
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Section sub-component
// ---------------------------------------------------------------------------

function Section(props: {
  title: string;
  items: Array<{
    key: string;
    left: React.ReactNode;
    right: React.ReactNode;
    edited: boolean;
    onEdit: () => void;
  }>;
}) {
  return (
    <div>
      <p className="text-sm font-semibold uppercase tracking-wide text-muted-foreground mb-1">
        {props.title}
      </p>
      <ul className="flex flex-col gap-1">
        {props.items.map((it) => (
          <li
            key={it.key}
            className="flex items-start justify-between gap-3 text-sm"
          >
            <span>
              {it.left}: {it.right}
              {it.edited && (
                <span className="ml-2 text-[10px] uppercase font-mono text-amber-500">
                  user-edited
                </span>
              )}
            </span>
            <button
              type="button"
              onClick={it.onEdit}
              className="shrink-0 text-muted-foreground hover:text-foreground"
              aria-label={`Edit ${it.key}`}
              title="Edit"
            >
              <Pencil className="h-3 w-3" />
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
