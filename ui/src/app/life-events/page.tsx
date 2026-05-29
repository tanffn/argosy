"use client";

/**
 * /life-events — structured-intake form for career / family / asset /
 * expense / recurring-expense / retirement-milestone events that feed:
 *
 *   - cashflow_projection.effective_retire_ready_age() clamp logic
 *     (sprint commit #9) — retirement_milestone:target_retire_year_change
 *     + blocking expense_event rows shift the retire-ready age.
 *   - HolisticTimelineCard on /retirement — every event is a marker.
 *   - Monitor agent — context (not trigger) for drift interpretation.
 *
 * Sprint commit #8 (spec cbf6a07 §4). Loud-error contract: the API returns
 * a structured 422 (`category_not_recognized` / `kind_not_valid_for_category`)
 * when the user submits an out-of-enum input. The form owns the 422
 * handler explicitly so the red banner renders inline instead of being
 * swallowed by a global error boundary.
 */

import { useEffect, useState } from "react";
import { AlertTriangle, Calendar, Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Banner } from "@/components/ui/banner";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  api,
  type LifeEventCategory,
  type LifeEventDTO,
  type LifeEventsCatalog,
  type LifeEventsCreateError,
  type LifeEventsCreateRequest,
} from "@/lib/api";

const USER_ID = "ariel";
const MAX_DESCRIPTION_CHARS = 280;

// Field-visibility rules are server-driven now (codex IMPORTANT #6 on
// commit #8 review): the catalog endpoint returns
// `field_rules_by_category[<cat>] = {requires_amount, supports_recurring_years}`.
// The UI consumes that map instead of hardcoded category sets so a new
// category added on the backend can't silently miss its field rendering.

function categoryLabel(c: string): string {
  // "career_event" -> "Career"
  return c
    .replace(/_event$/, "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (ch) => ch.toUpperCase());
}

function kindLabel(k: string): string {
  return k.replace(/_/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase());
}

interface FormState {
  category: LifeEventCategory | "";
  kind: string;
  target_date: string;
  amount_usd: string;
  recurring_years: string;
  description: string;
}

function emptyForm(): FormState {
  return {
    category: "",
    kind: "",
    target_date: "",
    amount_usd: "",
    recurring_years: "",
    description: "",
  };
}

/**
 * Sprint #2 commit #12 — read prefill_* query params from
 * window.location.search and merge into the empty form. Called as the
 * lazy initializer for ``useState<FormState>`` so the read happens
 * exactly once at mount, not via a synchronous setState in useEffect
 * (which lints as a cascading-render hazard).
 *
 * Returns the empty form when no prefill params are present, when
 * running server-side (window === undefined), or when the URL has no
 * query string.
 */
function formFromPrefill(): FormState {
  if (typeof window === "undefined") return emptyForm();
  const params = new URLSearchParams(window.location.search);
  const cat = params.get("prefill_category");
  const kind = params.get("prefill_kind");
  const date = params.get("prefill_date");
  const amount = params.get("prefill_amount");
  const description = params.get("prefill_description");
  if (!cat && !kind && !date && !amount && !description) return emptyForm();
  const base = emptyForm();
  return {
    ...base,
    category: (cat as LifeEventCategory | "") || base.category,
    kind: kind || base.kind,
    target_date: date || base.target_date,
    amount_usd: amount || base.amount_usd,
    description: description || base.description,
  };
}

export default function LifeEventsPage() {
  const [catalog, setCatalog] = useState<LifeEventsCatalog | null>(null);
  const [events, setEvents] = useState<LifeEventDTO[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [form, setForm] = useState<FormState>(formFromPrefill);
  const [submitting, setSubmitting] = useState(false);

  // Two error surfaces:
  //   loudError       -> the structured 422 banner (category / kind invalid)
  //   submitError     -> anything else (network, unknown 422, 5xx, ...)
  const [loudError, setLoudError] = useState<LifeEventsCreateError | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [cat, evs] = await Promise.all([
          api.lifeEventsCatalog(),
          api.lifeEventsList(USER_ID),
        ]);
        if (cancelled) return;
        setCatalog(cat);
        setEvents(evs);
      } catch (e: unknown) {
        if (cancelled) return;
        setLoadError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Derived dropdown options. `kinds` is empty until a category is picked,
  // which keeps the dependent dropdown disabled in the JSX below.
  const categories = catalog?.categories ?? [];
  const kinds =
    form.category && catalog
      ? catalog.kinds_by_category[form.category] ?? []
      : [];

  const categoryPicked = form.category !== "";
  const kindPicked = form.kind !== "";
  // Server-driven field visibility per spec §4 + codex IMPORTANT #6 on
  // commit #8 review. Catalog supplies the rule map; if a key is
  // missing (e.g. backend hasn't been deployed with the new field yet),
  // default to hidden — safe direction since the underlying field is
  // optional anyway.
  const fieldRules = categoryPicked && catalog
    ? catalog.field_rules_by_category[form.category] ?? {}
    : {};
  const showAmount = Boolean(fieldRules.requires_amount);
  const showRecurring = Boolean(fieldRules.supports_recurring_years);

  const canSubmit = categoryPicked && kindPicked && !submitting;

  const onCategoryChange = (value: string) => {
    // Category change always resets the kind: the new category's kind
    // list is a different set, so the previously-picked kind would fail
    // server validation. Better to force the user to repick than to ship
    // a silently invalid form.
    setForm((f) => ({
      ...f,
      category: value as LifeEventCategory | "",
      kind: "",
    }));
    setLoudError(null);
    setSubmitError(null);
  };

  const onKindChange = (value: string) => {
    setForm((f) => ({ ...f, kind: value }));
    setLoudError(null);
    setSubmitError(null);
  };

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setLoudError(null);
    setSubmitError(null);

    const payload: LifeEventsCreateRequest = {
      user_id: USER_ID,
      category: form.category as LifeEventCategory,
      kind: form.kind,
    };
    if (form.target_date) payload.target_date = form.target_date;
    if (showAmount && form.amount_usd.trim()) {
      const n = Number(form.amount_usd);
      if (!Number.isFinite(n) || n <= 0) {
        setSubmitError("Amount must be a positive number.");
        setSubmitting(false);
        return;
      }
      payload.amount_usd = n;
    }
    if (showRecurring && form.recurring_years.trim()) {
      const n = Number(form.recurring_years);
      if (!Number.isInteger(n) || n <= 0) {
        setSubmitError("Recurring years must be a positive integer.");
        setSubmitting(false);
        return;
      }
      payload.recurring_years = n;
    }
    if (form.description.trim()) {
      payload.description = form.description.trim();
    }

    try {
      const created = await api.lifeEventsCreate(payload);
      setEvents((prev) => [created, ...prev]);
      setForm(emptyForm());
    } catch (err: unknown) {
      // Pattern-match the structured loud-error first, then fall back to
      // a generic submit error. This is the explicit 422 handler the
      // codex BLOCKER on spec §4.1 requires.
      if (
        err &&
        typeof err === "object" &&
        "kind" in (err as Record<string, unknown>) &&
        ((err as { kind: string }).kind === "category_not_recognized" ||
          (err as { kind: string }).kind === "kind_not_valid_for_category")
      ) {
        setLoudError(err as LifeEventsCreateError);
      } else {
        setSubmitError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setSubmitting(false);
    }
  };

  const onDelete = async (id: number) => {
    try {
      await api.lifeEventsDelete(id, USER_ID);
      setEvents((prev) => prev.filter((e) => e.id !== id));
    } catch (e: unknown) {
      setSubmitError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <main className="max-w-5xl mx-auto p-6 flex flex-col gap-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight flex items-center gap-2">
          <Calendar className="h-6 w-6" aria-hidden suppressHydrationWarning />
          Life Events
        </h1>
        <p className="text-sm text-muted-foreground">
          Record career, family, asset, and large-expense events so the plan
          can clamp the retire-ready age, the Retirement timeline can render
          them as markers, and the Monitor agent can read them as context.
          The dropdowns are server-driven — pick a category, then a kind, then
          fill in the optional detail fields.
        </p>
      </header>

      {loudError ? <LoudErrorBanner error={loudError} /> : null}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">New life event</CardTitle>
          <CardDescription>
            Category and kind are required. The detail fields appear once a
            kind is picked.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? (
            <p className="text-sm text-muted-foreground">Loading catalog…</p>
          ) : loadError ? (
            <p className="text-sm text-error font-mono">{loadError}</p>
          ) : (
            <form onSubmit={onSubmit} className="flex flex-col gap-4">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <label className="flex flex-col gap-1">
                  <span className="text-xs font-medium text-muted-foreground">
                    Category <span className="text-error">*</span>
                  </span>
                  <select
                    value={form.category}
                    onChange={(e) => onCategoryChange(e.target.value)}
                    className="bg-background border border-border rounded-md px-3 py-1.5 text-sm"
                    required
                  >
                    <option value="">— pick a category —</option>
                    {categories.map((c) => (
                      <option key={c} value={c}>
                        {categoryLabel(c)}
                      </option>
                    ))}
                  </select>
                </label>

                <label className="flex flex-col gap-1">
                  <span className="text-xs font-medium text-muted-foreground">
                    Kind <span className="text-error">*</span>
                  </span>
                  <select
                    value={form.kind}
                    onChange={(e) => onKindChange(e.target.value)}
                    disabled={!categoryPicked}
                    className="bg-background border border-border rounded-md px-3 py-1.5 text-sm disabled:opacity-50 disabled:cursor-not-allowed"
                    required
                  >
                    <option value="">
                      {categoryPicked
                        ? "— pick a kind —"
                        : "pick a category first"}
                    </option>
                    {kinds.map((k) => (
                      <option key={k} value={k}>
                        {kindLabel(k)}
                      </option>
                    ))}
                  </select>
                </label>
              </div>

              {kindPicked ? (
                <>
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                    <label className="flex flex-col gap-1">
                      <span className="text-xs font-medium text-muted-foreground">
                        Target date (optional)
                      </span>
                      <Input
                        type="date"
                        value={form.target_date}
                        onChange={(e) =>
                          setForm((f) => ({ ...f, target_date: e.target.value }))
                        }
                      />
                    </label>

                    {showAmount ? (
                      <label className="flex flex-col gap-1">
                        <span className="text-xs font-medium text-muted-foreground">
                          Amount (USD, optional)
                        </span>
                        <Input
                          type="number"
                          inputMode="decimal"
                          step="0.01"
                          min="0"
                          placeholder="0.00"
                          value={form.amount_usd}
                          onChange={(e) =>
                            setForm((f) => ({
                              ...f,
                              amount_usd: e.target.value,
                            }))
                          }
                          className="font-mono"
                        />
                      </label>
                    ) : null}

                    {showRecurring ? (
                      <label className="flex flex-col gap-1">
                        <span className="text-xs font-medium text-muted-foreground">
                          Recurring years (optional)
                        </span>
                        <Input
                          type="number"
                          inputMode="numeric"
                          step="1"
                          min="1"
                          placeholder="e.g. 4"
                          value={form.recurring_years}
                          onChange={(e) =>
                            setForm((f) => ({
                              ...f,
                              recurring_years: e.target.value,
                            }))
                          }
                          className="font-mono"
                        />
                      </label>
                    ) : null}
                  </div>

                  <label className="flex flex-col gap-1">
                    <span className="text-xs font-medium text-muted-foreground">
                      Description (optional, up to {MAX_DESCRIPTION_CHARS} chars)
                    </span>
                    <textarea
                      value={form.description}
                      onChange={(e) =>
                        setForm((f) => ({
                          ...f,
                          description: e.target.value.slice(
                            0,
                            MAX_DESCRIPTION_CHARS,
                          ),
                        }))
                      }
                      rows={3}
                      placeholder="Free-form context for the Monitor agent / future-you."
                      className="bg-background border border-border rounded-md px-3 py-1.5 text-sm"
                    />
                    <span className="text-[10px] text-muted-foreground self-end">
                      {form.description.length}/{MAX_DESCRIPTION_CHARS}
                    </span>
                  </label>
                </>
              ) : null}

              {submitError ? (
                <p className="text-sm text-error font-mono">{submitError}</p>
              ) : null}

              <div className="flex justify-end">
                <Button type="submit" disabled={!canSubmit}>
                  {submitting ? "Saving…" : "Save life event"}
                </Button>
              </div>
            </form>
          )}
        </CardContent>
      </Card>

      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold tracking-wide uppercase text-muted-foreground">
          Recorded life events ({events.length})
        </h2>
        {events.length === 0 ? (
          <Card>
            <CardContent className="py-6">
              <p className="text-sm text-muted-foreground">
                No life events recorded yet. Once you save one, it&apos;ll show
                up here ordered by target date.
              </p>
            </CardContent>
          </Card>
        ) : (
          <div className="flex flex-col gap-2">
            {events.map((ev) => (
              <EventRow key={ev.id} event={ev} onDelete={onDelete} />
            ))}
          </div>
        )}
      </section>
    </main>
  );
}

function LoudErrorBanner({ error }: { error: LifeEventsCreateError }) {
  if (error.kind === "category_not_recognized") {
    return (
      <Banner
        tone="error"
        icon={<AlertTriangle className="h-4 w-4" />}
        title="Category not recognized"
      >
        <p>
          I don&apos;t have a category for{" "}
          <span className="font-mono">&apos;{error.input}&apos;</span>. Please
          pick one of:{" "}
          <span className="font-mono">
            {error.validCategories.join(", ")}
          </span>{" "}
          or open Advisor to discuss.
        </p>
      </Banner>
    );
  }
  return (
    <Banner
      tone="error"
      icon={<AlertTriangle className="h-4 w-4" />}
      title="Kind not valid for category"
    >
      <p>
        I don&apos;t have a kind <span className="font-mono">&apos;{error.input}&apos;</span>{" "}
        for that category. Please pick one of:{" "}
        <span className="font-mono">{error.validKinds.join(", ")}</span> or
        open Advisor to discuss.
      </p>
    </Banner>
  );
}

interface EventRowProps {
  event: LifeEventDTO;
  onDelete: (id: number) => void;
}

function EventRow({ event, onDelete }: EventRowProps) {
  return (
    <Card>
      <CardContent className="py-3">
        <div className="flex items-start justify-between gap-3">
          <div className="flex-1 min-w-0 flex flex-col gap-1">
            <div className="flex items-center gap-2 flex-wrap">
              <Badge variant="secondary" className="font-mono text-[10px]">
                {event.category}
              </Badge>
              <Badge variant="outline" className="font-mono text-[10px]">
                {event.kind}
              </Badge>
              {event.target_date ? (
                <span className="text-xs text-muted-foreground font-mono">
                  {event.target_date}
                </span>
              ) : (
                <span className="text-xs text-muted-foreground italic">
                  no target date
                </span>
              )}
              {event.amount_usd != null ? (
                <span className="text-xs font-mono text-foreground">
                  ${event.amount_usd.toLocaleString()}
                </span>
              ) : null}
              {event.recurring_years != null ? (
                <span className="text-xs font-mono text-muted-foreground">
                  × {event.recurring_years}y
                </span>
              ) : null}
            </div>
            {event.description ? (
              <p className="text-sm text-muted-foreground">
                {event.description}
              </p>
            ) : null}
          </div>
          <Button
            type="button"
            variant="outline"
            size="icon"
            onClick={() => onDelete(event.id)}
            title="Delete this life event"
          >
            <Trash2 className="h-4 w-4" />
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
