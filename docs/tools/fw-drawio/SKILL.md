---
name: fw-drawio
description: |
  Create draw.io XML diagrams. Provides XML template, style constants,
  diagram types, delta marking, verification impact rules, and export workflow.
  Generic - usable for FW, software architecture, or any flow/structure diagram.
  Use when: diagram, drawio, draw.io, block diagram, flow diagram, data flow, FSM diagram,
  swimlane, sequence diagram, delta flow, verification.
version: 1.3.1-argosy
allowed-tools: [Write, Read, Bash, Glob]
---

# Draw.io Diagram Skill

Create draw.io XML diagrams.

## Argosy adaptation notes (read first)

This skill ships with FW-team conventions. When using it on Argosy:

- **Path references** in this manifest were rewritten from `<AGATHA>/tools/dev/...`
  to `docs/tools/...` (the local layout).
- **Delta marking** (NEW / MOD / REMOVED color codes in §"Delta Marking
  Convention") applies when documenting *changes* to an existing system. For
  the Argosy SDD, which documents an existing system's architecture, use the
  default Blue palette throughout and skip the NEW/MOD/REMOVED prefixes.
- **Verification Impact Tables** (§"Verification Impact Rule") are FW
  practice. For Argosy SDD diagrams, replace with a one-line "Where to find
  it in code:" anchor or skip — the SDD itself plays the verification role.
- **`ag_drawio.py validate`** is referenced in §"Step 4 Verify completeness"
  but the script is NOT shipped with Argosy (broken `lib.output` import made
  it unusable here). Treat that step as "loud-fallback" → skip the validate
  invocation; rely on `drawio_export.py` to fail loudly if the XML is broken.

Everything else (XML template, style constants, color palette, edge routing
rules, geometry rules, legend conventions, fan-out routing) is fully reusable
as-is and battle-tested.

## Design Philosophy

Diagrams are for developers and verification teams, not presentations. They must:

1. **Show real FSM states and events.** Use actual state names from code, not abstract labels. Include event names that trigger transitions.
2. **Focus on the delta.** Highlight what is NEW or CHANGED. Use color to distinguish existing flow from new additions. Any developer should immediately see what this feature adds.
3. **Be professional and consistent.** Same font (Calibri), same sizes, same color palette across all diagrams in a project.
4. **Be self-contained.** A developer should understand the diagram without reading the spec. Include a legend if using color coding.

## Delta Marking Convention

| Element | Style |
|---------|-------|
| **Existing (unchanged)** | Blue fill (#E6F3FF), normal stroke |
| **New (added by this feature)** | Green fill (#E6FFE6), bold stroke (strokeWidth=2.5), label prefix: "[NEW]" |
| **Modified** | Orange fill (#FFF4E6), dashed stroke (dashed=1), label prefix: "[MOD]" |
| **Removed / deprecated** | Gray fill (#F5F5F5), dotted stroke, strikethrough text |

## Verification Impact Rule

**Diagrams are verification artifacts, not just documentation.** Every diagram that shows code changes must:

1. **Show ALL deltas in one diagram.** Not just the happy path.
2. **Show current vs new side by side.** Left column = current behavior, right column = new behavior.
3. **Include a Verification Impact Table** below the flow: what changed, where in code, what to test.
4. **Highlight fundamental behavioral changes** with red borders.
5. **Audience includes verification team.** Every testable change visible at a glance.

## Diagram Types

| Type | Use For | Style |
|------|---------|-------|
| `fsm_flow` | FSM states, transitions, events | Rounded rectangles = states, labeled edges = events/conditions |
| `delta_flow` | Current vs new behavior with all changes | Two-column: left=current, right=new, with verification table |
| `data_flow` | Step-by-step numbered flow | Numbered edges between process blocks |
| `swimlane` | Multi-actor flows (FW, HW, NOS) | Vertical swimlanes with time flowing top-to-bottom |
| `matrix` | Decision tables | Table-style grid cells with color-coded values |

## XML Template

```xml
<mxfile host="app.diagrams.net" modified="2026-04-15">
  <diagram id="UNIQUE_ID" name="Diagram Title">
    <mxGraphModel dx="1422" dy="762" grid="1" gridSize="10" guides="1"
                  tooltips="1" connect="1" arrows="1" fold="1"
                  page="1" pageScale="1" pageWidth="1400" pageHeight="820">
      <root>
        <mxCell id="0"/>
        <mxCell id="1" parent="0"/>
        <!-- All diagram cells go here -->
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
```

## Style Constants

### Swimlane Containers
```
style="swimlane;fontFamily=Calibri;fontSize=12;fillColor=<fc>;strokeColor=<ec>;strokeWidth=2;startSize=28;rounded=1;fontStyle=1;fontColor=#000000;"
```
**IMPORTANT:** Swimlane header values must be PLAIN TEXT. Do NOT use HTML tags in swimlane values. Use `fontStyle=1` for bold. HTML tags work ONLY in content cells with `html=1`.

### Content Cells
```
style="rounded=1;whiteSpace=wrap;html=1;fillColor=<fc>;strokeColor=<ec>;strokeWidth=1.5;fontColor=#000000;fontFamily=Calibri;fontSize=10;"
```

### Edges
```
style="edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;strokeColor=<c>;strokeWidth=1.5;fontColor=#000000;endArrow=classic;fontFamily=Calibri;fontSize=9;"
```

### Decision Diamond
```
style="rhombus;whiteSpace=wrap;html=1;fillColor=#FFF4E6;strokeColor=#E2A674;strokeWidth=1.5;fontColor=#000000;fontFamily=Calibri;fontSize=10;"
```

### Color Palette

| Color | fillColor | strokeColor | Use For |
|-------|-----------|-------------|---------|
| Blue | #E6F3FF | #4A90E2 | Primary blocks, modules |
| Green | #E6FFE6 | #74E274 | Success paths, active states, NEW |
| Orange | #FFF4E6 | #E2A674 | Decisions, warnings, MOD |
| Red | #FFE6E6 | #E27474 | Error paths, fault states |
| Purple | #F0E6FF | #B274E2 | External interfaces |
| Gray | #F5F5F5 | #999999 | Disabled, inactive, out of scope |

## Cell ID Convention

- Swimlanes: `swim_<actor>`
- Blocks: `blk_<name>`
- Decisions: `dec_<name>`
- Edges: `edge_<from>_<to>`

## Geometry Rules

- Page size: 1400 x 820 (default). Increase pageHeight if content overflows.
- **Page margins:** minimum 40px from all page edges. No group or block at x < 40.
- Block size: 120x40 minimum, 200x60 for text-heavy
- Vertical spacing: 20-30px between blocks
- Align blocks at same level to same Y coordinate
- Center title on page: x = (pageWidth - titleWidth) / 2

### Centering child groups under parents

Child groups must be visually centered under their parent, not left-aligned.
Calculate: `child_x = parent_x + (parent_width - child_width) / 2`

If a child is wider than its parent, center both on the page midpoint instead.

### Even distribution of sibling groups

When N groups share a horizontal row (e.g., 4 resource groups at the bottom):
1. Calculate total group width: `total = sum(group_widths)`
2. Calculate gap: `gap = (pageWidth - 2*margin - total) / (N - 1)`
3. Place first group at `x = margin`, each subsequent at `prev_x + prev_width + gap`

Do NOT hardcode positions. Recalculate if groups are added or resized.

### Consistent block widths

All blocks in the same lane or group must have the **same width**. Pick one width per lane and apply it to every block in that lane. Mixed widths look sloppy and break visual alignment.

Example: if sub-agent blocks are 210px wide, then `blk_hld_writer`, `blk_research_agents`, `blk_code_study_agents` must ALL be 210px wide.

### Nothing outside its container

Every block must fit inside its parent swimlane or group. Before finishing:
1. Check that no block's Y + height exceeds its swimlane's Y + height
2. Check that no block's X + width exceeds its swimlane's X + width
3. If a block overflows, increase the container size, not the block position

### Straight edges, no unnecessary bends

Orthogonal edges between two blocks at the same Y should be a single horizontal line. Between two blocks at the same X should be a single vertical line. If the auto-router adds bends:
- Set explicit exit/entry anchor points (exitX, exitY, entryX, entryY)
- Or use explicit waypoints to force the path
- Never accept a zigzag when a straight line is possible

### Fan-out edge routing (1 source -> N targets)

When one source connects to 3+ targets in a row below, **never rely on auto-routing** -- edges will stack on top of each other or cross unpredictably.

Instead, use explicit waypoints through an intermediate Y coordinate:

```xml
<!-- Fan-out: skills group -> 4 resource groups below -->
<!-- Left target: route through intermediate Y, then left -->
<mxCell id="edge_to_left" edge="1" source="grp_skills" target="grp_vdi">
  <mxGeometry relative="1" as="geometry">
    <Array as="points">
      <mxPoint x="700" y="510"/>   <!-- center X, intermediate Y -->
      <mxPoint x="280" y="510"/>   <!-- target center X, same Y -->
    </Array>
  </mxGeometry>
</mxCell>
<!-- Right target: route through intermediate Y, then right -->
<mxCell id="edge_to_right" edge="1" source="grp_skills" target="grp_casa">
  <mxGeometry relative="1" as="geometry">
    <Array as="points">
      <mxPoint x="700" y="510"/>
      <mxPoint x="1090" y="510"/>
    </Array>
  </mxGeometry>
</mxCell>
<!-- Center targets: direct drop, no waypoints needed -->
```

The pattern: edges exit the source center, travel to an intermediate Y (halfway between source bottom and target top), then fan out horizontally to each target's center X before dropping down. This creates clean L-shaped routes that never cross.

## Edge Routing Rules

**Orthogonal auto-routing is unreliable when edges cross swimlane groups.**
Always use explicit waypoints for edges that must pass other panels.

### Rule 1: Bypass pattern (edge must cross a group to reach target)

Route the edge out to the margin, travel vertically along the margin, then enter the target.
Use the **right margin** (x = pageWidth + 20) or **left margin** (x = 30) depending on layout.

```xml
<!-- Dispatch: Orchestrator right side -> right margin -> down to Phase 7 -->
<mxCell id="edge_dispatch" style="html=1;strokeColor=#4A90E2;strokeWidth=2;endArrow=classic;rounded=1;"
        edge="1" source="blk_parent" target="grp_phase7" parent="1">
  <mxGeometry relative="1" as="geometry">
    <Array as="points">
      <mxPoint x="1570" y="480"/>   <!-- exit right, stay at source Y -->
      <mxPoint x="1570" y="960"/>   <!-- travel down along right margin -->
    </Array>
  </mxGeometry>
</mxCell>

<!-- Return: Phase 7 right side -> right margin (offset +20) -> up to Orchestrator -->
<mxCell id="edge_return" style="html=1;strokeColor=#999999;strokeWidth=1.5;endArrow=classic;dashed=1;rounded=1;"
        edge="1" source="grp_phase7" target="blk_parent" parent="1">
  <mxGeometry relative="1" as="geometry">
    <Array as="points">
      <mxPoint x="1590" y="973"/>   <!-- offset from dispatch by 20px -->
      <mxPoint x="1590" y="470"/>   <!-- travel up along right margin -->
    </Array>
  </mxGeometry>
</mxCell>
```

**Key rules:**
- Dispatch and return use the **same margin side** but offset by 20px (e.g., 1570 vs 1590)
- Both edges are visible, parallel, never overlapping
- The bypass is symmetric: clean L-shape or U-shape

### Rule 2: Parallel dispatch/return on the same side

When two edges connect the same pair (dispatch + return), separate them:

| Pattern | Dispatch | Return |
|---------|----------|--------|
| **Vertical** (top/bottom) | exitX=0.4, entryX=0.5 | exitX=0.55, entryX=0.65 |
| **Horizontal** (left/right) | exitY=0.4, sourcePoint Y | exitY=0.6, sourcePoint Y+10 |
| **Bypass** (margin route) | margin at x=M | margin at x=M+20 |

For horizontal edges at the same Y level, use explicit source/target points instead of exit/entry percentages to avoid zigzag:

```xml
<!-- Dispatch: fixed point to fixed point, no auto-routing artifacts -->
<mxCell id="edge_dispatch" style="edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;strokeColor=#4A90E2;"
        edge="1" source="blk_parent" parent="1">
  <mxGeometry relative="1" as="geometry">
    <mxPoint x="320" y="470" as="targetPoint"/>
  </mxGeometry>
</mxCell>

<!-- Return: 10px below dispatch, same horizontal line -->
<mxCell id="edge_return" style="edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;strokeColor=#999999;dashed=1;"
        edge="1" target="blk_parent" parent="1">
  <mxGeometry relative="1" as="geometry">
    <mxPoint x="320" y="480" as="sourcePoint"/>
  </mxGeometry>
</mxCell>
```

### Rule 3: Convergence waypoints

When multiple edges from different sources converge to one target, route them through a shared horizontal line before turning to the target. This creates a clean fan-in pattern:

```xml
<!-- Lead 2 and Lead 3 both route through Y=160 before entering cross-reference block -->
<mxCell id="edge_lead2" style="edgeStyle=orthogonalEdgeStyle;rounded=1;" edge="1"
        source="blk_lead2" target="blk_xref" parent="grp_phase6">
  <mxGeometry relative="1" as="geometry">
    <Array as="points">
      <mxPoint x="455" y="160"/>   <!-- lead2 center X, shared Y -->
      <mxPoint x="528" y="160"/>   <!-- target entry X, shared Y -->
    </Array>
  </mxGeometry>
</mxCell>
```

### Rule 4: Precise entry with entryPerimeter=0

For exact positioning on rounded blocks, disable perimeter snapping:

```xml
entryX=0.667;entryY=-0.012;entryDx=0;entryDy=0;entryPerimeter=0;
```

This places the arrow at exactly 66.7% from the left edge. Without `entryPerimeter=0`, the arrow snaps to the nearest perimeter point and may shift.

## Legend Rules

1. **Calculate frame height:** `startSize + (item_count * (item_height + gap)) + bottom_padding`. Do not eyeball.
2. **All legend items must be children of the legend frame** (parent=legend_frame). Items outside the frame are clipped on export.
3. **Horizontal legends** for 4+ items to save vertical space: one row of colored boxes, one row of arrow descriptions.

## Dynamic Agent Counts

1. **Never hardcode agent counts** in deployment/architecture diagrams. Use "x N" or "x M" with example instances.
2. **Show the pattern:** 2-3 concrete examples + "..." ellipsis + "Agent N/M" (dashed border). This communicates "the count varies per feature."
3. **Each agent instance should show its specific output file path**, not just the parent folder.

## Output Location

`.drawio` source: `<FOLDER>/hld/diagrams/<name>.drawio`
`.png` export: `<FOLDER>/hld/diagrams/<name>.png` (next to the source, flat layout — pandoc resolves the relative path correctly when the markdown is later converted to docx).

Legacy projects with PNGs in `PNG_Output/`: move them up one level, or pass `--out-subdir PNG_Output` to `drawio_export.py`.

---

## Post-Creation Workflow (MANDATORY)

**Every diagram creation MUST complete all 4 steps below. A drawio file without PNG export, HLD embedding, and ASCII cleanup is INCOMPLETE.**

### Step 1: Export to PNG

Run the export tool immediately after creating the drawio (pass the directory; add `--file` for a single diagram):

```bash
python "docs/tools/drawio_export.py" "<FOLDER>/hld/diagrams"
python "docs/tools/drawio_export.py" "<FOLDER>/hld/diagrams" --file <name>.drawio
```

Output: `<FOLDER>/hld/diagrams/<name>.png`.

If `drawio_export.py` is not available or fails (e.g., no headless Chromium):
- Note it as a manual step
- Tell the user: *"PNG export pending — run `drawio_export.py` or export manually from draw.io (File → Export as → PNG, 2x scale)."*
- Do NOT skip steps 2-4

### Step 2: Embed PNG in the HLD

In the HLD markdown, embed the PNG image so the diagram renders inline:

```markdown
![<Diagram Title>](diagrams/<name>.png)

*Source: [<name>.drawio](diagrams/<name>.drawio) — open in draw.io to edit*
```

**Path must be `diagrams/<name>.png`** (not `diagrams/PNG_Output/<name>.png`). Pandoc silently drops images it can't find at the literal path; flat layout is what `md_to_docx.py` resolves.

**If PNG export was skipped** (step 1 failed), embed a placeholder:

```markdown
![<Diagram Title>](diagrams/<name>.png) *(PNG export pending)*

*Source: [<name>.drawio](diagrams/<name>.drawio) — open in draw.io to edit*
```

### Step 3: Replace ASCII art

If the HLD contains ASCII art (text-based flow diagrams, box-and-arrow drawings) that the new drawio replaces, **DELETE the ASCII art entirely**.

This is a CLAUDE_RULES requirement (FILE_ORGANIZATION): *"All diagrams → draw.io `.drawio` files. No ASCII art."*

**Check for:** code blocks with `│`, `▼`, `┌`, `└`, `→`, box-drawing characters, or any multi-line text layout that represents a flow or structure.

### Step 4: Verify completeness

Run `ag_drawio.py validate` on every authored `.drawio`. The script does the
structural / edge-target / mxgraph-compat checks deterministically, and emits
a single `[RESULT] cells=N edges=M mxgraph_compat=true|false` line the skill
can grep:

```bash
python "docs/tools/ag_drawio.py" validate "<FOLDER>/hld/diagrams/<name>.drawio"
```

Exit codes: 0 = valid + compat, 2 = invalid (missing cells, broken edge
target, no `<mxGraphModel>`, etc.), 3 = mxgraph-version incompatibility.

Plus the four post-export checks the script does NOT cover (these stay
prose because they cross file boundaries — markdown ↔ diagrams):

- [ ] Every `.drawio` has a corresponding `.png` next to it in `diagrams/` (or export is noted as pending)
- [ ] Every `.png` is embedded in the HLD with `![](...)` syntax
- [ ] No ASCII art remains for any diagrammed flow
- [ ] Drawio source is referenced as editable link below each PNG
- [ ] Diagram references in the HLD match actual file names

#### Loud-fallback

If `ag_drawio.py validate` is missing or fails:

1. **Surface the failure verbatim** — print the script's stderr, exit code,
   and the command line you ran to the user.
2. **Announce the regression explicitly:**
   > Tool `ag_drawio.py validate` failed. This is a regression — the tool
   > worked before and is now broken for everyone, not just this session.
3. **Demand a fix:**
   > Run `/agatha-tech-support` after this session ends. Describe the
   > failure so the tool can be fixed and the lesson logged.
4. **Offer manual prose fallback ONLY after explicit user "use prose path"** —
   then walk the user through the structural checks by hand (open the
   `.drawio` in draw.io, count vertices and edges, verify no dangling
   arrows). Log to `<FOLDER>/decisions/tool_fallback_ag_drawio.md`.

This matches the `MCP_HEALTH_AND_AUTH` rule extended to tools: surface every
error, never silent fall-back.
