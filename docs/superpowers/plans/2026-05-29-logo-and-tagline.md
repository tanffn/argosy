# Logo + tagline — image-gen prompts and tagline candidates

User-driven decision required. I generated these autonomously per
the overnight mandate but explicitly didn't pick — per the saved
"autonomous overnight" memory: *"Logo / brand decisions are an
exception: do NOT autonomously pick a logo style. Generate text
prompts for image-gen instead, leave the final pick to him."*

## Brand context (for your image-gen tool)

Argosy = a fleet of merchant ships in archaic English (Shakespeare's
*Merchant of Venice* uses the word). The product is:
- A **multi-agent** financial advisor (think: fleet of specialists,
  not one big model)
- **Paper-mode by default** — risk-averse, manual approvals
- **Audit-trail by design** — every decision persisted, replayable

Current logo: a `🚢` emoji + the wordmark "Argosy" in monospace.
That's it. Replacement should be inline-SVG (so it scales + plays
nice with the nav header at 1400×900) or a transparent PNG at 64×64
that can sit next to the wordmark.

The brand-hero card on Home already uses a Lucide **Anchor** icon
in a tinted-green square (`border-success/30 bg-success/10
text-success`). The new logo should NOT clash with that anchor —
maybe the nav-logo uses a different mark and the anchor stays
exclusive to the hero, OR the new logo IS an anchor and the hero
loses it.

## Image-gen prompts (pick one as a starting point; iterate)

**Prompt 1 — Stylized fleet (recommended):**
> "Minimalist geometric logo of three stylized merchant-ship sails
> arranged in a triangular fleet formation, viewed from above.
> Monochromatic emerald-green (#10b981) on dark navy background.
> Sharp clean vector lines, no gradients, no text. Square icon
> suitable for app nav header at 64×64 pixels. Modern fintech
> aesthetic, evokes navigation and coordination."

**Prompt 2 — Compass + ship hybrid:**
> "Logo combining a ship's wheel (helm) and a compass rose into a
> single geometric mark. Clean line-art, emerald-green and slate
> only, no shading. Suggests both navigation and coordination of
> multiple agents. Vector style, scales from 16px favicon to 256px
> hero. Square aspect ratio."

**Prompt 3 — Anchor + sextant:**
> "Modern minimalist logo of an anchor crossed with a sextant
> (navigation tool), unified into a single mark. Single-line stroke
> art, emerald accent on a dark slate background. Suggests
> seafaring + precise measurement. Square icon, vector, no text."

**Prompt 4 — Abstract fleet trail:**
> "Abstract logo: three small triangular sails leaving a single
> wave-line trail behind them, suggesting forward motion in
> coordinated formation. Vector style, emerald-on-slate, very
> minimal — no more than 4 distinct shapes. Reads cleanly at 32px."

**Prompt 5 — Constellation:**
> "Logo of 4-5 stylized ship icons arranged like a constellation,
> connected by faint dotted lines. Each ship is just a small
> triangle. Emerald lines on dark slate background. Evokes a fleet
> navigating together; the dotted lines suggest the multi-agent
> coordination."

## Tagline candidates

Current: *"multi-agent financial advisor — paper-mode by default,
audit-trail by design"*

Concrete enough — it tells you what the product is and what its
safety posture is — but heavy. Six lighter candidates ordered by
how much they lean on the Argosy brand metaphor:

1. **"A fleet of agents at your helm."** (Brand-heavy. Implies
   user is captain, the agent fleet is the crew.)

2. **"Multi-agent financial navigation."** (Brand-light. Most
   literal. Easy to grasp.)

3. **"Your wealth, with a fleet behind it."** (Brand-medium.
   Reassuring; implies the agents work FOR you.)

4. **"Every decision charted. Every voyage logged."** (Brand-heavy.
   Captures audit-trail + chart-as-plan double-meaning. Sells the
   safety posture.)

5. **"Audit-trail by design. Paper-mode by default."** (Brand-zero.
   Just the safety guarantees, snappier than current.)

6. **"A fleet for the long voyage."** (Brand-heavy, retirement-
   leaning. Implies the long-horizon nature of the FIRE planning.)

My read: **#1 ("A fleet of agents at your helm")** and **#4
("Every decision charted. Every voyage logged.")** are the two
strongest. #1 is punchy for the brand-hero card; #4 reads better
as a sub-line under the wordmark.

## What I did NOT change

I didn't touch the 🚢 emoji in either `ui/src/components/nav.tsx`
or `ui/src/app/page.tsx` (the brand-hero card). Both still ship
the existing wordmark + emoji combination. When you've generated
a logo you like, the swap is mechanical:

  - `ui/src/components/nav.tsx` line ~75: replace
    `<span ...>🚢</span>` with `<Image src="/logo.svg" .../>` or an
    inline SVG.
  - `ui/src/app/page.tsx` brand-hero card around line 590: same
    pattern, larger size.

I didn't autonomously generate a PNG via codex either — the
codex-tandem kit's `engine_codex` defaults to `--sandbox read-only`
in this fork, and image generation isn't a documented capability.
The prompts above are the deliverable.
