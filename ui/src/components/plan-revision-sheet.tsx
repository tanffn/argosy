"use client";

import { useState } from "react";
import { Check, Star, X } from "lucide-react";

import { Markdown } from "@/components/markdown";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  api,
  type DeltaItem,
  type DraftResponse,
  type HorizonView,
} from "@/lib/api";
import { friendlySourceLabel } from "@/lib/plain-english-labels";

interface PlanRevisionSheetProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  userId: string;
  draft: DraftResponse;
  onAccepted: () => void;
  onRejected: () => void;
}

export function PlanRevisionSheet(props: PlanRevisionSheetProps) {
  const { open, onOpenChange, userId, draft, onAccepted, onRejected } = props;

  const [activeTab, setActiveTab] = useState<
    "deltas" | "long" | "medium" | "short"
  >("deltas");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const allDeltas: DeltaItem[] = [
    ...(draft.horizon_long?.deltas_from_prior ?? []),
    ...(draft.horizon_medium?.deltas_from_prior ?? []),
    ...(draft.horizon_short?.deltas_from_prior ?? []),
  ];

  const acceptDelta = async (item: DeltaItem) => {
    setError(null);
    setWorking(true);
    try {
      await api.planDraftDeltaAccept(draft.plan_version_id, item.item_id, userId);
      // Local mutate (the sheet re-renders on parent re-fetch via WS).
      item.accepted = true;
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setWorking(false);
    }
  };

  const acceptAll = async () => {
    setError(null);
    setWorking(true);
    try {
      await api.planDraftAccept(draft.plan_version_id, userId);
      onAccepted();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setWorking(false);
    }
  };

  const reject = async () => {
    const reason = window.prompt("What should the fleet reconsider?") ?? "";
    if (!reason) return;
    setError(null);
    setWorking(true);
    try {
      await api.planDraftReject(draft.plan_version_id, userId, reason);
      onRejected();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setWorking(false);
    }
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        className="w-full sm:max-w-2xl overflow-y-auto"
      >
        <SheetHeader>
          <SheetTitle>Monthly plan revision</SheetTitle>
          <SheetDescription>
            Synthesized {new Date(draft.drafted_at).toLocaleString()} · derived
            from baseline #{draft.derived_from_id} · run{" "}
            {draft.decision_run_id != null ? `#${draft.decision_run_id}` : null}
          </SheetDescription>
        </SheetHeader>

        {error && (
          <p className="text-sm text-error font-mono mt-3">{error}</p>
        )}

        <Tabs
          value={activeTab}
          onValueChange={(v) => setActiveTab(v as typeof activeTab)}
          className="mt-4"
        >
          <TabsList>
            <TabsTrigger value="deltas">
              Deltas ({allDeltas.length})
            </TabsTrigger>
            <TabsTrigger value="long">Long</TabsTrigger>
            <TabsTrigger value="medium">
              <span className="flex items-center gap-1">
                Medium <Star className="h-3 w-3 text-warning" />
              </span>
            </TabsTrigger>
            <TabsTrigger value="short">Short</TabsTrigger>
          </TabsList>

          <TabsContent value="deltas">
            <DeltasView
              deltas={allDeltas}
              onAccept={acceptDelta}
              disabled={working}
            />
          </TabsContent>
          <TabsContent value="long">
            <HorizonViewBlock
              h={draft.horizon_long}
              md={draft.horizon_long_md}
            />
          </TabsContent>
          <TabsContent value="medium">
            <HorizonViewBlock
              h={draft.horizon_medium}
              md={draft.horizon_medium_md}
            />
          </TabsContent>
          <TabsContent value="short">
            <HorizonViewBlock
              h={draft.horizon_short}
              md={draft.horizon_short_md}
            />
            {draft.horizon_short &&
            draft.horizon_short.speculative_candidates.length > 0 ? (
              <div className="mt-4">
                <p className="text-sm font-semibold uppercase tracking-wide text-muted-foreground mb-1">
                  Speculative candidates (bounded-risk)
                </p>
                <ul className="flex flex-col gap-2">
                  {draft.horizon_short.speculative_candidates.map((c, i) => (
                    <li
                      key={i}
                      className="border border-info/30 rounded-md p-2 text-sm"
                    >
                      <div className="flex items-center justify-between">
                        <strong>{c.ticker}</strong>
                        <span className="text-xs font-mono text-muted-foreground">
                          ≤ ${c.suggested_position_usd.toLocaleString()} ·{" "}
                          {(c.suggested_position_pct_of_net_worth * 100).toFixed(
                            2,
                          )}
                          % NW
                        </span>
                      </div>
                      <p className="text-xs text-muted-foreground">
                        {c.thesis_summary}
                      </p>
                      <p className="text-[10px] font-mono text-muted-foreground">
                        exit: {c.exit_trigger}
                      </p>
                    </li>
                  ))}
                </ul>
                <p className="text-xs text-muted-foreground italic mt-2">
                  Worth a small swing if you want it. Take action from the
                  Argonaut tab.
                </p>
              </div>
            ) : null}
          </TabsContent>
        </Tabs>

        <div className="mt-6 flex justify-between gap-2 sticky bottom-0 bg-background py-2">
          <Button variant="outline" onClick={reject} disabled={working}>
            <X className="h-4 w-4 mr-1" /> Reject + re-synthesize
          </Button>
          <Button onClick={acceptAll} disabled={working}>
            <Check className="h-4 w-4 mr-1" /> Accept all remaining
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function DeltasView(props: {
  deltas: DeltaItem[];
  onAccept: (d: DeltaItem) => void | Promise<void>;
  disabled: boolean;
}) {
  if (props.deltas.length === 0) {
    return (
      <div className="py-6 text-center text-sm text-muted-foreground">
        No changes recommended this month — the fleet thinks current state is
        fine.
      </div>
    );
  }
  return (
    <ul className="flex flex-col gap-3 mt-3">
      {props.deltas.map((d) => (
        <li
          key={`${d.horizon}.${d.item_id}`}
          className="border border-border rounded-md p-3"
        >
          <div className="flex items-start justify-between gap-2">
            <div className="text-sm">
              <span className="text-xs uppercase font-mono text-muted-foreground mr-2">
                [{d.horizon}]
              </span>
              <strong>{d.summary}</strong>
            </div>
            <div className="flex gap-1">
              {d.accepted ? (
                <span className="text-xs text-success font-mono">
                  accepted
                </span>
              ) : (
                <button
                  type="button"
                  onClick={() => props.onAccept(d)}
                  disabled={props.disabled}
                  className="text-xs text-primary hover:underline"
                >
                  Accept
                </button>
              )}
            </div>
          </div>
          {d.rationale && (
            <p className="text-xs text-muted-foreground mt-1">{d.rationale}</p>
          )}
          {d.cited_sources.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-1">
              {d.cited_sources.map((c) => (
                <span
                  key={c}
                  className="text-[10px] bg-accent/40 px-1.5 py-0.5 rounded"
                  title={c}
                >
                  {friendlySourceLabel(c)}
                </span>
              ))}
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

function HorizonViewBlock(props: {
  h: HorizonView | null;
  md: string | null;
}) {
  if (!props.h) {
    return (
      <div className="py-6 text-center text-sm text-muted-foreground">
        Empty horizon.
      </div>
    );
  }
  // Wave 8 Piece E — render the horizon markdown through the shared
  // <Markdown> component (react-markdown + remark-gfm) instead of a
  // raw <pre> dump so headings, lists, tables, and code fences format
  // like prose. The repo's <Markdown> wrapper handles XSS by relying
  // on react-markdown's default safe-mode (no raw HTML rendering).
  return (
    <div className="mt-3">
      <Markdown>{props.md ?? ""}</Markdown>
    </div>
  );
}
