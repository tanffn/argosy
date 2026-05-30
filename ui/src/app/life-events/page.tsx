"use client";

/**
 * /life-events — cashflow-phase modeler (Spec D commit #5).
 *
 * The form is reorganized around the three CASHFLOW SHAPES per spec
 * §4.1 — not the legacy "pick a category, then a kind, then maybe a
 * date and an amount" wizard. Each section gates the catalog dropdowns
 * (category, kind) and the per-shape fields by the server-supplied
 * `required_fields_by_delta_kind` map (spec §3.2), so a new shape /
 * category added on the backend cannot silently miss its rendering.
 *
 *   One-shot                  → delta_kind=one_shot
 *   Recurring                 → delta_kind=recurring_every_n_years
 *   Phase change              → delta_kind=phase_change_start (open-ended)
 *                               or phase_change_end (closed band) when
 *                               the optional "Ends" date is filled in.
 *
 * Sign convention (spec §1.2): all amounts are SIGNED in the wire
 * payload. Positive = income / expense-reduction, negative = expense /
 * income-loss. The UI presents this as a single signed number field
 * with a helper line ("positive = income, negative = expense") so the
 * user doesn't have to look up the table. The cashflow engine reads
 * the signed amount directly (one site only — see
 * `_apply_signed_delta_to_series` in cashflow_projection.py).
 *
 * 422-banner contract (spec §4.3, preserved from commit #8) — four
 * structured-error variants render an inline red banner above the
 * sections:
 *
 *   - category_not_recognized            (pre-existing)
 *   - kind_not_valid_for_category        (pre-existing)
 *   - delta_kind_not_valid_for_category  (spec D #4 — new)
 *   - delta_shape_invalid                (spec D #4 — new)
 *
 * Migration UX (spec §1.5):
 *
 *   - Acknowledge banner — top-of-page persistent info banner gated on
 *     `users.life_events_migration_acknowledged_at IS NULL`. Dismiss
 *     button POSTs the acknowledgment (degrades to client-side dismiss
 *     if the backend route is not yet wired).
 *   - Conversion-assistant modal — fires when the user clicks an
 *     existing event whose `delta_kind='none'` AND whose description
 *     carries the migration marker ("Originally a ...") that the
 *     migration script wrote per spec BLOCKER #1. Offers to re-classify
 *     as one_shot / recurring / phase_change by DELETEing the legacy
 *     row + POSTing a new one.
 */

import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Calendar,
  Info,
  RefreshCw,
  Trash2,
} from "lucide-react";

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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  api,
  type LifeEventCategory,
  type LifeEventDTO,
  type LifeEventDeltaKind,
  type LifeEventsCatalog,
  type LifeEventsCreateError,
  type LifeEventsCreateRequest,
} from "@/lib/api";

const USER_ID = "ariel";
const MAX_DESCRIPTION_CHARS = 280;

// Migration marker that the alembic 0054 data-migration step writes
// into the description column of every legacy row converted to
// delta_kind=none. Used to detect "this is a converted row that the
// user may want to re-classify" — see ConversionAssistantModal below.
const MIGRATION_MARKER_REGEX = /Originally a/i;

// ---------------------------------------------------------------------
// Section discriminator + per-section state
// ---------------------------------------------------------------------

type SectionId = "one_shot" | "recurring" | "phase_change";

interface SectionMeta {
  id: SectionId;
  title: string;
  blurb: string;
  // The default delta_kind for this section. "phase_change" picks
  // _start by default; the user opts into _end by typing an "Ends" date.
  defaultDeltaKind: LifeEventDeltaKind;
}

const SECTIONS: readonly SectionMeta[] = [
  {
    id: "one_shot",
    title: "One-shot expenses or income",
    blurb:
      "Single event on a specific date. Wedding gift, inheritance, RSU vest landing as cash.",
    defaultDeltaKind: "one_shot",
  },
  {
    id: "recurring",
    title: "Recurring expenses",
    blurb:
      "Periodic spike anchored on a date, every N years. New car / renovation / family travel.",
    defaultDeltaKind: "recurring_every_n_years",
  },
  {
    id: "phase_change",
    title: "Phase changes (when life patterns shift)",
    blurb:
      "Step function — monthly cashflow shifts from a start date onward. Kids leave home, partner retires.",
    defaultDeltaKind: "phase_change_start",
  },
] as const;

// Map the section id to the list of delta_kinds it admits. Phase change
// covers both _start (open-ended) and _end (closed band).
const ALLOWED_DELTA_KINDS_BY_SECTION: Record<SectionId, LifeEventDeltaKind[]> = {
  one_shot: ["one_shot"],
  recurring: ["recurring_every_n_years"],
  phase_change: ["phase_change_start", "phase_change_end"],
};

// Inverse — given a delta_kind, which section does the prefill drop into.
function sectionForDeltaKind(dk: LifeEventDeltaKind): SectionId {
  if (dk === "one_shot") return "one_shot";
  if (dk === "recurring_every_n_years") return "recurring";
  return "phase_change";
}

// Per-section form state. We carry the union of all fields and let the
// section's `effectiveDeltaKind()` + the server's required-field map
// decide which ones are sent on submit. Strings everywhere so number /
// date inputs stay controlled without juggling NaN.
interface SectionFormState {
  category: LifeEventCategory | "";
  kind: string;
  description: string;
  // one_shot
  one_shot_amount_usd: string;
  one_shot_date: string;
  // recurring
  recurring_amount_usd: string;
  recurring_period_years: string;
  recurring_anchor_date: string;
  // phase change
  monthly_delta_usd: string;
  phase_start_date: string;
  phase_end_date: string;
}

function emptySection(): SectionFormState {
  return {
    category: "",
    kind: "",
    description: "",
    one_shot_amount_usd: "",
    one_shot_date: "",
    recurring_amount_usd: "",
    recurring_period_years: "",
    recurring_anchor_date: "",
    monthly_delta_usd: "",
    phase_start_date: "",
    phase_end_date: "",
  };
}

function categoryLabel(c: string): string {
  return c
    .replace(/_event$/, "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (ch) => ch.toUpperCase());
}

function kindLabel(k: string): string {
  return k.replace(/_/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase());
}

// ---------------------------------------------------------------------
// Prefill — Spec D §4.3
// ---------------------------------------------------------------------

interface PrefillResult {
  section: SectionId | null;
  state: Partial<SectionFormState>;
}

/**
 * Read prefill_* query params and project them into the matching
 * section. The new contract (Spec D §4.3 — UpcomingVestCard) sends
 * `section=one_shot` + `prefill_delta_kind` + per-shape amounts.
 *
 * Pre-Spec-D query params (`prefill_date` / `prefill_amount` /
 * `prefill_kind` without delta_kind) are still honored for any in-
 * flight bookmark or external link — they hydrate the one_shot
 * section as the conservative fallback.
 */
function prefillFromUrl(): PrefillResult {
  if (typeof window === "undefined") return { section: null, state: {} };
  const params = new URLSearchParams(window.location.search);
  const cat = params.get("prefill_category");
  const kind = params.get("prefill_kind");
  const description = params.get("prefill_description");
  const sectionParam = params.get("section");
  const deltaKindParam = params.get("prefill_delta_kind");
  const oneShotAmount = params.get("prefill_one_shot_amount_usd");
  const oneShotDate =
    params.get("prefill_one_shot_date") || params.get("prefill_target_date");
  // Legacy fallback (pre-Spec-D).
  const legacyDate = params.get("prefill_date");
  const legacyAmount = params.get("prefill_amount");

  const haveAnything =
    cat ||
    kind ||
    description ||
    sectionParam ||
    deltaKindParam ||
    oneShotAmount ||
    oneShotDate ||
    legacyDate ||
    legacyAmount;
  if (!haveAnything) return { section: null, state: {} };

  // Pick the section from `section=` first, then `prefill_delta_kind=`,
  // then fall back to one_shot (legacy contract).
  let section: SectionId;
  if (sectionParam === "one_shot") section = "one_shot";
  else if (sectionParam === "recurring") section = "recurring";
  else if (sectionParam === "phase_change") section = "phase_change";
  else if (deltaKindParam) {
    section = sectionForDeltaKind(deltaKindParam as LifeEventDeltaKind);
  } else {
    section = "one_shot";
  }

  const state: Partial<SectionFormState> = {};
  if (cat) state.category = cat as LifeEventCategory;
  if (kind) state.kind = kind;
  if (description) state.description = description;
  if (section === "one_shot") {
    if (oneShotAmount) state.one_shot_amount_usd = oneShotAmount;
    else if (legacyAmount) state.one_shot_amount_usd = legacyAmount;
    if (oneShotDate) state.one_shot_date = oneShotDate;
    else if (legacyDate) state.one_shot_date = legacyDate;
  } else if (section === "recurring") {
    if (oneShotAmount) state.recurring_amount_usd = oneShotAmount;
    if (oneShotDate) state.recurring_anchor_date = oneShotDate;
  } else {
    // phase_change
    if (oneShotDate) state.phase_start_date = oneShotDate;
  }
  return { section, state };
}

// ---------------------------------------------------------------------
// Page component
// ---------------------------------------------------------------------

export default function LifeEventsPage() {
  const [catalog, setCatalog] = useState<LifeEventsCatalog | null>(null);
  const [events, setEvents] = useState<LifeEventDTO[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Per-section form state — three independent forms, one per section.
  // The prefill initializer pre-populates the matching section only.
  const [oneShotForm, setOneShotForm] = useState<SectionFormState>(() => {
    const r = prefillFromUrl();
    return r.section === "one_shot"
      ? { ...emptySection(), ...r.state }
      : emptySection();
  });
  const [recurringForm, setRecurringForm] = useState<SectionFormState>(() => {
    const r = prefillFromUrl();
    return r.section === "recurring"
      ? { ...emptySection(), ...r.state }
      : emptySection();
  });
  const [phaseForm, setPhaseForm] = useState<SectionFormState>(() => {
    const r = prefillFromUrl();
    return r.section === "phase_change"
      ? { ...emptySection(), ...r.state }
      : emptySection();
  });

  const [submittingSection, setSubmittingSection] = useState<SectionId | null>(
    null,
  );
  const [loudError, setLoudError] = useState<LifeEventsCreateError | null>(
    null,
  );
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Acknowledge-banner state (spec §1.5). Visible when the user has at
  // least one migration_log row whose corresponding event still carries
  // the marker. We approximate "has unacknowledged migration rows" by
  // scanning the events list for the migration marker AND assume the
  // banner is dismissable client-side until the backend route lands.
  const [bannerDismissed, setBannerDismissed] = useState(false);
  const showMigrationBanner = useMemo(
    () =>
      !bannerDismissed &&
      events.some(
        (ev) =>
          ev.delta_kind === "none" &&
          ev.description != null &&
          MIGRATION_MARKER_REGEX.test(ev.description),
      ),
    [events, bannerDismissed],
  );

  // Conversion-assistant modal state.
  const [conversionTarget, setConversionTarget] = useState<LifeEventDTO | null>(
    null,
  );

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

  // Scroll the target section into view + focus its first input when
  // the page lands with `?section=...`. Runs once after the catalog
  // resolves (so the section's DOM is rendered).
  useEffect(() => {
    if (loading || typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const sectionParam = params.get("section");
    if (!sectionParam) return;
    const target = document.getElementById(`life-event-section-${sectionParam}`);
    if (!target) return;
    target.scrollIntoView({ behavior: "smooth", block: "start" });
    const firstInput = target.querySelector<HTMLElement>(
      "select, input, textarea",
    );
    firstInput?.focus();
  }, [loading]);

  const resetForm = (id: SectionId) => {
    if (id === "one_shot") setOneShotForm(emptySection());
    else if (id === "recurring") setRecurringForm(emptySection());
    else setPhaseForm(emptySection());
  };

  const onSubmit = async (id: SectionId, form: SectionFormState) => {
    if (!catalog) return;
    if (form.category === "" || form.kind === "") {
      setSubmitError("Pick a category and kind first.");
      return;
    }
    setSubmittingSection(id);
    setLoudError(null);
    setSubmitError(null);

    const payload = buildPayload(id, form);
    if (payload === null) {
      // buildPayload set the submit error.
      setSubmittingSection(null);
      return;
    }

    // Server-driven pre-validation: refuse to send a payload that's
    // missing a required field per `required_fields_by_delta_kind`.
    // The server enforces the same rule (loud-error 422); doing it
    // client-side avoids the round-trip on common typos.
    const required =
      catalog.required_fields_by_delta_kind[payload.delta_kind ?? "none"] ??
      [];
    const missing = required.filter(
      (f) => payload[f as keyof typeof payload] == null,
    );
    if (missing.length > 0) {
      setSubmitError(
        `Missing required fields for ${payload.delta_kind}: ${missing.join(", ")}.`,
      );
      setSubmittingSection(null);
      return;
    }

    // Cross-section check: the picked category must allow this
    // section's delta_kind per spec §1.4 matrix.
    const allowed =
      catalog.delta_kind_rules_by_category[payload.category]?.allowed_delta_kinds ??
      [];
    if (!allowed.includes(payload.delta_kind!)) {
      setSubmitError(
        `Category "${payload.category}" doesn't support ${payload.delta_kind} (allowed: ${allowed.join(", ")}). Move to a different section.`,
      );
      setSubmittingSection(null);
      return;
    }

    try {
      const created = await api.lifeEventsCreate(payload);
      setEvents((prev) => [created, ...prev]);
      resetForm(id);
    } catch (err: unknown) {
      if (
        err &&
        typeof err === "object" &&
        "kind" in (err as Record<string, unknown>) &&
        typeof (err as { kind: unknown }).kind === "string"
      ) {
        const k = (err as { kind: string }).kind;
        if (
          k === "category_not_recognized" ||
          k === "kind_not_valid_for_category" ||
          k === "delta_kind_not_valid_for_category" ||
          k === "delta_shape_invalid"
        ) {
          setLoudError(err as LifeEventsCreateError);
        } else {
          setSubmitError(err instanceof Error ? err.message : String(err));
        }
      } else {
        setSubmitError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setSubmittingSection(null);
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

  const onAcknowledgeMigration = async () => {
    // Optimistic client-side dismiss. If the backend route is wired,
    // we record it; if not (404 / network), the banner still goes
    // away for this session — the info isn't load-bearing.
    setBannerDismissed(true);
    try {
      await api.acknowledgeLifeEventsMigration(USER_ID);
    } catch {
      // intentionally swallowed — see docstring on the api method
    }
  };

  const onOpenConversionAssistant = (ev: LifeEventDTO) => {
    setConversionTarget(ev);
  };

  const onConfirmConversion = async (
    target: LifeEventDTO,
    sectionId: SectionId,
  ) => {
    // The modal collects the same per-section form values; on confirm
    // it DELETEs the legacy row + POSTs a new one with the chosen
    // shape. The shape transition is deliberately not atomic on the
    // server (LifeEventUpdateRequest refuses cross-shape transitions)
    // so we sequence the two ops here.
    try {
      // Build the new row by hand; the modal already validated.
      const created = await api.lifeEventsCreate(
        buildPayloadFromConversion(target, sectionId),
      );
      await api.lifeEventsDelete(target.id, USER_ID);
      setEvents((prev) => [
        created,
        ...prev.filter((e) => e.id !== target.id),
      ]);
      setConversionTarget(null);
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
          Record how your cashflow changes across phases of life. Pick the
          shape that best matches the event — a single dated spike, a
          periodic spike every few years, or a step-function shift in
          your monthly baseline. The plan re-composes from the new shape.
        </p>
      </header>

      {showMigrationBanner ? (
        <Banner
          tone="info"
          icon={<Info className="h-4 w-4" />}
          title="Life events were converted to the new cashflow-shape model"
        >
          <p>
            Existing events from the previous schema were migrated. Some
            rows carry the marker &ldquo;Originally a&hellip;&rdquo; in
            their description — click <em>Re-classify</em> on those rows
            to upgrade them to the right cashflow shape.
          </p>
          <div className="mt-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={onAcknowledgeMigration}
            >
              I&apos;ve reviewed all conversions
            </Button>
          </div>
        </Banner>
      ) : null}

      {loudError ? <LoudErrorBanner error={loudError} /> : null}

      {loading ? (
        <p className="text-sm text-muted-foreground">Loading catalog…</p>
      ) : loadError ? (
        <p className="text-sm text-error font-mono">{loadError}</p>
      ) : catalog === null ? (
        <p className="text-sm text-error font-mono">
          Catalog missing — cannot render the form.
        </p>
      ) : (
        <>
          <SectionCard
            id="one_shot"
            meta={SECTIONS[0]}
            form={oneShotForm}
            setForm={setOneShotForm}
            catalog={catalog}
            submitting={submittingSection === "one_shot"}
            onSubmit={() => onSubmit("one_shot", oneShotForm)}
          />
          <SectionCard
            id="recurring"
            meta={SECTIONS[1]}
            form={recurringForm}
            setForm={setRecurringForm}
            catalog={catalog}
            submitting={submittingSection === "recurring"}
            onSubmit={() => onSubmit("recurring", recurringForm)}
          />
          <SectionCard
            id="phase_change"
            meta={SECTIONS[2]}
            form={phaseForm}
            setForm={setPhaseForm}
            catalog={catalog}
            submitting={submittingSection === "phase_change"}
            onSubmit={() => onSubmit("phase_change", phaseForm)}
          />

          {submitError ? (
            <p className="text-sm text-error font-mono">{submitError}</p>
          ) : null}

          <section className="flex flex-col gap-3">
            <h2 className="text-sm font-semibold tracking-wide uppercase text-muted-foreground">
              Recorded life events ({events.length})
            </h2>
            {events.length === 0 ? (
              <Card>
                <CardContent className="py-6">
                  <p className="text-sm text-muted-foreground">
                    No life events recorded yet. Once you save one,
                    it&apos;ll show up here ordered by date.
                  </p>
                </CardContent>
              </Card>
            ) : (
              <div className="flex flex-col gap-2">
                {events.map((ev) => (
                  <EventRow
                    key={ev.id}
                    event={ev}
                    onDelete={onDelete}
                    onOpenConversionAssistant={onOpenConversionAssistant}
                  />
                ))}
              </div>
            )}
          </section>
        </>
      )}

      <ConversionAssistantModal
        // Re-key on target.id so the modal's internal section-pick
        // state resets when a different row is opened (cheaper than
        // a setState-in-effect reset which trips the
        // react-hooks/set-state-in-effect lint rule).
        key={conversionTarget?.id ?? "closed"}
        target={conversionTarget}
        catalog={catalog}
        onCancel={() => setConversionTarget(null)}
        onConfirm={onConfirmConversion}
      />
    </main>
  );
}

// ---------------------------------------------------------------------
// SectionCard — one of the three top-level forms
// ---------------------------------------------------------------------

interface SectionCardProps {
  id: SectionId;
  meta: SectionMeta;
  form: SectionFormState;
  setForm: React.Dispatch<React.SetStateAction<SectionFormState>>;
  catalog: LifeEventsCatalog;
  submitting: boolean;
  onSubmit: () => void;
}

function SectionCard({
  id,
  meta,
  form,
  setForm,
  catalog,
  submitting,
  onSubmit,
}: SectionCardProps) {
  // Filter the category dropdown to those whose allowed_delta_kinds
  // intersects this section's set per spec §1.4.
  const allowedForSection = ALLOWED_DELTA_KINDS_BY_SECTION[id];
  const categories = catalog.categories.filter((c) => {
    const allowed = catalog.delta_kind_rules_by_category[c]?.allowed_delta_kinds;
    return allowed?.some((dk) => allowedForSection.includes(dk));
  });

  const categoryPicked = form.category !== "";
  const kinds = categoryPicked ? catalog.kinds_by_category[form.category] ?? [] : [];

  const nudge = categoryPicked
    ? catalog.delta_kind_rules_by_category[form.category]?.nudge
    : null;

  // Effective delta_kind drives field visibility. For the phase-change
  // section, the user upgrades from `_start` (open-ended) to `_end`
  // (closed) by typing an "Ends" date; we expose both shapes through
  // the same UI.
  const effectiveDeltaKind = effectiveDeltaKindFor(id, form);

  // Server-driven required-field map — drives field rendering.
  const required = catalog.required_fields_by_delta_kind[effectiveDeltaKind] ?? [];
  const shows = (fieldName: string) => required.includes(fieldName);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    onSubmit();
  };

  return (
    <Card id={`life-event-section-${id}`}>
      <CardHeader>
        <CardTitle className="text-base">{meta.title}</CardTitle>
        <CardDescription>{meta.blurb}</CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="flex flex-col gap-4">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-muted-foreground">
                Category <span className="text-error">*</span>
              </span>
              <select
                value={form.category}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    category: e.target.value as LifeEventCategory | "",
                    kind: "",
                  }))
                }
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
                onChange={(e) =>
                  setForm((f) => ({ ...f, kind: e.target.value }))
                }
                disabled={!categoryPicked}
                className="bg-background border border-border rounded-md px-3 py-1.5 text-sm disabled:opacity-50 disabled:cursor-not-allowed"
                required
              >
                <option value="">
                  {categoryPicked ? "— pick a kind —" : "pick a category first"}
                </option>
                {kinds.map((k) => (
                  <option key={k} value={k}>
                    {kindLabel(k)}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {nudge ? (
            <p className="text-[11px] text-muted-foreground italic">{nudge}</p>
          ) : null}

          {/* One-shot fields. */}
          {shows("one_shot_amount_usd") || shows("target_date") ? (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {shows("target_date") ? (
                <label className="flex flex-col gap-1">
                  <span className="text-xs font-medium text-muted-foreground">
                    Date <span className="text-error">*</span>
                  </span>
                  <Input
                    type="date"
                    value={form.one_shot_date}
                    onChange={(e) =>
                      setForm((f) => ({ ...f, one_shot_date: e.target.value }))
                    }
                    required
                  />
                </label>
              ) : null}
              {shows("one_shot_amount_usd") ? (
                <SignedAmountField
                  label="Amount (USD)"
                  value={form.one_shot_amount_usd}
                  onChange={(v) =>
                    setForm((f) => ({ ...f, one_shot_amount_usd: v }))
                  }
                />
              ) : null}
            </div>
          ) : null}

          {/* Recurring fields. */}
          {shows("recurring_amount_usd") ? (
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <SignedAmountField
                label="Amount per occurrence (USD)"
                value={form.recurring_amount_usd}
                onChange={(v) =>
                  setForm((f) => ({ ...f, recurring_amount_usd: v }))
                }
              />
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-muted-foreground">
                  Period (years) <span className="text-error">*</span>
                </span>
                <Input
                  type="number"
                  inputMode="numeric"
                  step="1"
                  min="1"
                  placeholder="e.g. 5"
                  value={form.recurring_period_years}
                  onChange={(e) =>
                    setForm((f) => ({
                      ...f,
                      recurring_period_years: e.target.value,
                    }))
                  }
                  className="font-mono"
                  required
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-muted-foreground">
                  First occurrence <span className="text-error">*</span>
                </span>
                <Input
                  type="date"
                  value={form.recurring_anchor_date}
                  onChange={(e) =>
                    setForm((f) => ({
                      ...f,
                      recurring_anchor_date: e.target.value,
                    }))
                  }
                  required
                />
              </label>
            </div>
          ) : null}

          {/* Phase-change fields. */}
          {shows("phase_start_date") ? (
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <SignedAmountField
                label="Monthly delta (USD)"
                value={form.monthly_delta_usd}
                onChange={(v) =>
                  setForm((f) => ({ ...f, monthly_delta_usd: v }))
                }
                helper="positive = monthly income or expense reduction; negative = monthly expense or income loss"
              />
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-muted-foreground">
                  Starts <span className="text-error">*</span>
                </span>
                <Input
                  type="date"
                  value={form.phase_start_date}
                  onChange={(e) =>
                    setForm((f) => ({
                      ...f,
                      phase_start_date: e.target.value,
                    }))
                  }
                  required
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-muted-foreground">
                  Ends (optional)
                </span>
                <Input
                  type="date"
                  value={form.phase_end_date}
                  onChange={(e) =>
                    setForm((f) => ({
                      ...f,
                      phase_end_date: e.target.value,
                    }))
                  }
                />
                <span className="text-[10px] text-muted-foreground">
                  Leave blank for open-ended.
                </span>
              </label>
            </div>
          ) : null}

          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-muted-foreground">
              Description (optional, up to {MAX_DESCRIPTION_CHARS} chars)
            </span>
            <textarea
              value={form.description}
              onChange={(e) =>
                setForm((f) => ({
                  ...f,
                  description: e.target.value.slice(0, MAX_DESCRIPTION_CHARS),
                }))
              }
              rows={2}
              placeholder="Free-form context for the Monitor agent / future-you."
              className="bg-background border border-border rounded-md px-3 py-1.5 text-sm"
            />
            <span className="text-[10px] text-muted-foreground self-end">
              {form.description.length}/{MAX_DESCRIPTION_CHARS}
            </span>
          </label>

          <div className="flex justify-end">
            <Button
              type="submit"
              disabled={submitting || !categoryPicked || form.kind === ""}
            >
              {submitting ? "Saving…" : "Save"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

function SignedAmountField({
  label,
  value,
  onChange,
  helper,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  helper?: string;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs font-medium text-muted-foreground">
        {label} <span className="text-error">*</span>
      </span>
      <Input
        type="number"
        inputMode="decimal"
        step="0.01"
        placeholder="e.g. +50000 or -67000"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="font-mono"
        required
      />
      <span className="text-[10px] text-muted-foreground">
        {helper ?? "positive = income / expense reduction; negative = expense / income loss"}
      </span>
    </label>
  );
}

// ---------------------------------------------------------------------
// Helpers — section / form → payload
// ---------------------------------------------------------------------

function effectiveDeltaKindFor(
  id: SectionId,
  form: SectionFormState,
): LifeEventDeltaKind {
  if (id === "one_shot") return "one_shot";
  if (id === "recurring") return "recurring_every_n_years";
  // phase_change — upgrade to _end iff the user filled in the optional
  // end-date field.
  return form.phase_end_date.trim() !== ""
    ? "phase_change_end"
    : "phase_change_start";
}

function parseFloatStrict(s: string): number | null {
  const t = s.trim();
  if (t === "") return null;
  const n = Number(t);
  if (!Number.isFinite(n)) return null;
  return n;
}

function parseIntStrict(s: string): number | null {
  const t = s.trim();
  if (t === "") return null;
  const n = Number(t);
  if (!Number.isInteger(n) || n <= 0) return null;
  return n;
}

function buildPayload(
  id: SectionId,
  form: SectionFormState,
): LifeEventsCreateRequest | null {
  const dk = effectiveDeltaKindFor(id, form);
  const payload: LifeEventsCreateRequest = {
    user_id: USER_ID,
    category: form.category as LifeEventCategory,
    kind: form.kind,
    delta_kind: dk,
  };
  if (form.description.trim()) payload.description = form.description.trim();

  if (id === "one_shot") {
    const amt = parseFloatStrict(form.one_shot_amount_usd);
    if (amt === null) return null;
    payload.one_shot_amount_usd = amt;
    if (form.one_shot_date) payload.target_date = form.one_shot_date;
  } else if (id === "recurring") {
    const amt = parseFloatStrict(form.recurring_amount_usd);
    if (amt === null) return null;
    const period = parseIntStrict(form.recurring_period_years);
    if (period === null) return null;
    payload.recurring_amount_usd = amt;
    payload.recurring_period_years = period;
    if (form.recurring_anchor_date)
      payload.target_date = form.recurring_anchor_date;
  } else {
    // phase_change
    const amt = parseFloatStrict(form.monthly_delta_usd);
    if (amt === null) return null;
    payload.monthly_delta_usd = amt;
    if (form.phase_start_date)
      payload.phase_start_date = form.phase_start_date;
    if (form.phase_end_date) payload.phase_end_date = form.phase_end_date;
  }
  return payload;
}

function buildPayloadFromConversion(
  target: LifeEventDTO,
  sectionId: SectionId,
): LifeEventsCreateRequest {
  // Best-effort hydration of the new row from the legacy fields. The
  // user gets the chance to confirm before this lands.
  const payload: LifeEventsCreateRequest = {
    user_id: target.user_id,
    category: target.category as LifeEventCategory,
    kind: target.kind,
    delta_kind:
      sectionId === "one_shot"
        ? "one_shot"
        : sectionId === "recurring"
          ? "recurring_every_n_years"
          : "phase_change_start",
  };
  if (target.description) payload.description = target.description;
  if (sectionId === "one_shot") {
    payload.one_shot_amount_usd = target.amount_usd ?? 0;
    if (target.target_date) payload.target_date = target.target_date;
  } else if (sectionId === "recurring") {
    payload.recurring_amount_usd = target.amount_usd ?? 0;
    payload.recurring_period_years = target.recurring_years ?? 1;
    if (target.target_date) payload.target_date = target.target_date;
  } else {
    payload.monthly_delta_usd = 0;
    if (target.target_date) payload.phase_start_date = target.target_date;
  }
  return payload;
}

// ---------------------------------------------------------------------
// LoudErrorBanner — four 422 variants (Spec D §3.3)
// ---------------------------------------------------------------------

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
  if (error.kind === "kind_not_valid_for_category") {
    return (
      <Banner
        tone="error"
        icon={<AlertTriangle className="h-4 w-4" />}
        title="Kind not valid for category"
      >
        <p>
          I don&apos;t have a kind{" "}
          <span className="font-mono">&apos;{error.input}&apos;</span> for that
          category. Please pick one of:{" "}
          <span className="font-mono">{error.validKinds.join(", ")}</span> or
          open Advisor to discuss.
        </p>
      </Banner>
    );
  }
  if (error.kind === "delta_kind_not_valid_for_category") {
    return (
      <Banner
        tone="error"
        icon={<AlertTriangle className="h-4 w-4" />}
        title="Shape not valid for this category"
      >
        <p>
          The category{" "}
          <span className="font-mono">&apos;{error.category}&apos;</span> doesn&apos;t
          support shape{" "}
          <span className="font-mono">&apos;{error.deltaKind}&apos;</span>.
          Move this event to another section — allowed shapes are{" "}
          <span className="font-mono">
            {error.allowedDeltaKinds.join(", ")}
          </span>
          .
        </p>
      </Banner>
    );
  }
  // delta_shape_invalid
  return (
    <Banner
      tone="error"
      icon={<AlertTriangle className="h-4 w-4" />}
      title="Per-shape field check failed"
    >
      <p>
        The payload for shape{" "}
        <span className="font-mono">&apos;{error.deltaKind}&apos;</span> was
        rejected: {error.reason === "missing_required" ? "missing required" : "forbidden"}{" "}
        fields{" "}
        <span className="font-mono">
          {(error.reason === "missing_required"
            ? error.missingFields
            : error.forbiddenFields
          ).join(", ")}
        </span>
        .
      </p>
    </Banner>
  );
}

// ---------------------------------------------------------------------
// EventRow — shape-aware rendering (Spec D §4.4)
// ---------------------------------------------------------------------

interface EventRowProps {
  event: LifeEventDTO;
  onDelete: (id: number) => void;
  onOpenConversionAssistant: (ev: LifeEventDTO) => void;
}

function EventRow({
  event,
  onDelete,
  onOpenConversionAssistant,
}: EventRowProps) {
  const canReclassify =
    event.delta_kind === "none" &&
    event.description != null &&
    MIGRATION_MARKER_REGEX.test(event.description);

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
              <Badge variant="outline" className="font-mono text-[10px]">
                {event.delta_kind}
              </Badge>
              <EventShapeDescriptor event={event} />
            </div>
            {event.description ? (
              <p className="text-sm text-muted-foreground whitespace-pre-line">
                {event.description}
              </p>
            ) : null}
          </div>
          <div className="flex items-center gap-2">
            {canReclassify ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => onOpenConversionAssistant(event)}
                title="This row was migrated from the legacy schema — re-classify into the new cashflow-shape model"
              >
                <RefreshCw className="h-3 w-3 mr-1" />
                Re-classify
              </Button>
            ) : null}
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
        </div>
      </CardContent>
    </Card>
  );
}

function EventShapeDescriptor({ event }: { event: LifeEventDTO }) {
  // Per spec §4.4 — render the shape-effective fields, not just the
  // raw legacy date. Inclusive-month copy ("from August 2034 onward")
  // for phase changes per codex IMPORTANT #3.
  if (event.delta_kind === "one_shot") {
    return (
      <>
        {event.target_date ? (
          <span className="text-xs text-muted-foreground font-mono">
            {event.target_date}
          </span>
        ) : null}
        {event.one_shot_amount_usd != null ? (
          <span className="text-xs font-mono text-foreground">
            {signedUsd(event.one_shot_amount_usd)}
          </span>
        ) : event.amount_usd != null ? (
          <span className="text-xs font-mono text-muted-foreground">
            ${event.amount_usd.toLocaleString()}
          </span>
        ) : null}
      </>
    );
  }
  if (event.delta_kind === "recurring_every_n_years") {
    return (
      <>
        {event.recurring_period_years != null ? (
          <span className="text-xs text-muted-foreground font-mono">
            every {event.recurring_period_years}y
          </span>
        ) : null}
        {event.target_date ? (
          <span className="text-xs text-muted-foreground font-mono">
            first {event.target_date}
          </span>
        ) : null}
        {event.recurring_amount_usd != null ? (
          <span className="text-xs font-mono text-foreground">
            {signedUsd(event.recurring_amount_usd)}
          </span>
        ) : null}
      </>
    );
  }
  if (event.delta_kind === "phase_change_start") {
    return (
      <>
        {event.phase_start_date ? (
          <span className="text-xs text-muted-foreground">
            from {monthLabel(event.phase_start_date)} onward (open-ended)
          </span>
        ) : null}
        {event.monthly_delta_usd != null ? (
          <span className="text-xs font-mono text-foreground">
            {signedUsd(event.monthly_delta_usd)}/mo
          </span>
        ) : null}
      </>
    );
  }
  if (event.delta_kind === "phase_change_end") {
    return (
      <>
        {event.phase_start_date && event.phase_end_date ? (
          <span className="text-xs text-muted-foreground">
            {monthLabel(event.phase_start_date)} →{" "}
            {monthLabel(event.phase_end_date)}
          </span>
        ) : null}
        {event.monthly_delta_usd != null ? (
          <span className="text-xs font-mono text-foreground">
            {signedUsd(event.monthly_delta_usd)}/mo
          </span>
        ) : null}
      </>
    );
  }
  // none
  return (
    <span className="text-xs text-muted-foreground italic">
      no cashflow effect
    </span>
  );
}

function signedUsd(n: number): string {
  const sign = n >= 0 ? "+" : "−";
  return `${sign}$${Math.abs(n).toLocaleString()}`;
}

function monthLabel(iso: string): string {
  // iso = "YYYY-MM-DD" — render as "Month YYYY"
  const d = new Date(iso + "T00:00:00Z");
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });
}

// ---------------------------------------------------------------------
// ConversionAssistantModal — codex IMPORTANT #2 / spec §1.5
// ---------------------------------------------------------------------

interface ConversionAssistantModalProps {
  target: LifeEventDTO | null;
  catalog: LifeEventsCatalog | null;
  onCancel: () => void;
  onConfirm: (target: LifeEventDTO, sectionId: SectionId) => void;
}

function ConversionAssistantModal({
  target,
  catalog,
  onCancel,
  onConfirm,
}: ConversionAssistantModalProps) {
  // Note: the page re-keys this component on target.id so the section
  // pick auto-resets when a different row is opened — no useEffect
  // setState needed (see react-hooks/set-state-in-effect lint rule).
  const [section, setSection] = useState<SectionId>("one_shot");

  if (!target) {
    return (
      <Dialog open={false} onOpenChange={onCancel}>
        <DialogContent />
      </Dialog>
    );
  }

  // Which sections are valid for this row's category per the §1.4 matrix.
  const allowed =
    catalog?.delta_kind_rules_by_category[target.category]?.allowed_delta_kinds ??
    [];
  const validSections = SECTIONS.filter((s) =>
    ALLOWED_DELTA_KINDS_BY_SECTION[s.id].some((dk) => allowed.includes(dk)),
  );

  return (
    <Dialog open={true} onOpenChange={onCancel}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Re-classify legacy life event</DialogTitle>
          <DialogDescription>
            This row was migrated from the previous schema with no
            cashflow effect (delta_kind=none). Pick the shape that best
            matches the original intent. The legacy row will be
            replaced — original description preserved.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <p className="text-xs text-muted-foreground">
            <span className="font-mono">{target.category}</span>{" "}
            <span className="font-mono">/ {target.kind}</span>
            {target.target_date ? (
              <>
                {" "}
                · originally dated{" "}
                <span className="font-mono">{target.target_date}</span>
              </>
            ) : null}
            {target.amount_usd != null ? (
              <>
                {" "}
                · originally ${target.amount_usd.toLocaleString()}
              </>
            ) : null}
          </p>

          <fieldset className="flex flex-col gap-2">
            <legend className="text-xs font-medium text-muted-foreground">
              Convert to
            </legend>
            {validSections.map((s) => (
              <label key={s.id} className="flex items-start gap-2 text-sm">
                <input
                  type="radio"
                  name="conversion-section"
                  value={s.id}
                  checked={section === s.id}
                  onChange={() => setSection(s.id)}
                  className="mt-0.5"
                />
                <span>
                  <span className="font-medium">{s.title}</span>
                  <span className="block text-xs text-muted-foreground">
                    {s.blurb}
                  </span>
                </span>
              </label>
            ))}
            {validSections.length === 0 ? (
              <p className="text-xs text-error">
                No shape is valid for this category — keep the row as
                delta_kind=none or delete it.
              </p>
            ) : null}
          </fieldset>
        </div>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            type="button"
            disabled={validSections.length === 0}
            onClick={() => onConfirm(target, section)}
          >
            Re-classify
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
