# Design System — TAA Tracker

## Product Context
- **What this is:** Per-antigen scorecard reference site for tumour-associated antigens. Modality cross-cut framing, AI-synthesized narratives grounded in cited sources, weekly snapshot.
- **Who it's for:** Biotech business development / corporate development / strategy. The person at a Genmab or Daiichi deciding whether to in-license, partner on, or pass on an antigen program.
- **Space/industry:** Biotech competitive intelligence, oncology / immuno-oncology.
- **Project type:** Static reference site (Python pipeline → JSON in git → Jinja-rendered HTML → Cloudflare Pages). Not a dashboard, not a SaaS, not a consumer site.

## Memorable Thing
> "The modality cross-cut grid is the moment — you SEE the competitive landscape at once."

Every design decision below serves this. Cross-cut grid is the visual hero on every scorecard, before tables, before prose. Inverts category convention (Open Targets / HPA / Cortellis bury data viz under tables).

## Aesthetic Direction
- **Direction:** Editorial / scientific-modern. Stripe Atlas reports × Distill × FT alphaville charts, pulled toward reference density.
- **Decoration level:** Intentional — hairline 1px dividers, subtle vertical rule grid only. No decorative blobs, gradients, illustrations, or stock photography.
- **Mood:** A scientific-financial publication a BD analyst wants to screenshot and Slack to their team. Authoritative, dense, beautiful enough to share.
- **Reference sites researched:** Open Targets (platform.opentargets.org), Human Protein Atlas (proteinatlas.org), Distill (distill.pub), Pudding (pudding.cool).

## Typography

All fonts are free Google Fonts loaded via `<link>`.

- **Display (antigen names, section heads, modality labels):** **Source Serif 4** — variable serif (200-900 weight, 8-60 optical size). Pharmaceutical-publication feel; reads cleaner than Fraunces at display sizes; familiar to BD/medical readers.
- **Body / UI:** **Instrument Sans** — clean modern sans, weights 400/500/600. Pairs naturally with Source Serif 4.
- **Citations / NCT / drug codes / accession numbers:** **JetBrains Mono** — for IDs only, never prose.

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,500;8..60,600;8..60,700&family=Instrument+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
```

### Type scale

| Role                       | Font            | Weight | Size      | Line-height | Notes |
|----------------------------|-----------------|--------|-----------|-------------|-------|
| Hero antigen name          | Source Serif 4  | 600    | 80px      | 0.95        | `font-variation-settings: "opsz" 60`, `letter-spacing: -0.02em` |
| Aggregate page hero        | Source Serif 4  | 600    | 56px      | 1.0         | "Whitespace", "What changed this week" etc. |
| Section heading            | Source Serif 4  | 600    | 28px      | 1.2         | `opsz 36` |
| Modality label (cross-cut) | Source Serif 4  | 600    | 17px      | 1.2         | `opsz 24` |
| Body / synthesis paragraph | Instrument Sans | 400    | 16-18px   | 1.6         | 18px in synthesis, 16px elsewhere |
| Quick stats value          | JetBrains Mono  | 500    | 18px      | 1.2         | `font-feature-settings: "tnum"` |
| Table header (uppercase)   | Instrument Sans | 500    | 11px      | 1.4         | `text-transform: uppercase`, `letter-spacing: 0.06em` |
| Table cell                 | Instrument Sans | 400    | 14px      | 1.5         | `font-feature-settings: "tnum"` for numeric columns |
| Citation marker `[12]`     | JetBrains Mono  | 500    | 11px      | 1           | `vertical-align: 3px` |
| ID (NCT, PMID, accession)  | JetBrains Mono  | 400    | 13px      | 1.4         | `font-feature-settings: "tnum"` |
| Caption / timestamp        | Instrument Sans | 400    | 12px      | 1.4         | `color: var(--muted)` |

## Color

**Approach:** Restrained. Mostly black/white/warm-gray with a single ink-blue accent for links/citations and a warm rust accent for active/positive status. Modality colors live ONLY inside the cross-cut grid and per-section markers.

### CSS custom properties

```css
:root {
  /* Surfaces */
  --bg: #FAFAF7;          /* warm off-white — paper feel */
  --surface: #FFFFFF;     /* cards, tables */
  --border: #E8E5DD;      /* warm hairline 1px */
  --border-strong: #D4D0C4;

  /* Text */
  --text: #0A0A0B;        /* near-black */
  --muted: #6B6B72;

  /* Accents */
  --accent: #1B2541;      /* ink blue — links, citations */
  --active: #B65140;      /* warm rust — active/positive */

  /* Semantic */
  --success: #2D6A4F;
  --warning: #B5803C;     /* shares ochre with bispecific modality */
  --error:   #9B2C2C;
  --info:    #1B2541;

  /* Modality (cross-cut grid only) */
  --m-adc:   #B65140;     /* rust */
  --m-cart:  #3A6B6E;     /* teal */
  --m-bisp:  #B5803C;     /* ochre */
  --m-mab:   #4A4A52;     /* graphite */
  --m-rdli:  #5B3F67;     /* deep purple */
  --m-vacc:  #7A8050;     /* olive */
}

[data-theme="dark"] {
  --bg: #16151A;          /* warm dark, NOT pure black */
  --surface: #1F1E24;
  --border: #2D2C33;
  --border-strong: #3D3C44;
  --text: #F5F2EA;        /* warm off-white */
  --muted: #9D9A92;
  --accent: #A8B5D0;
  --active: #D4694F;
  /* Modality colors carry over unchanged — already muted enough for dark */
}
```

### Color rules
- **Background is warm off-white (`#FAFAF7`), never pure white.** Reads as publication, not app.
- **Single accent color** for links and citations: ink blue `#1B2541`.
- **Active / positive** (approved status, "+3 this week" deltas): warm rust `#B65140`.
- **Modality colors NEVER appear outside the cross-cut grid + per-section markers.** They are not a general palette. Bullets, dividers, icons all use neutrals.
- **Dark mode is warm dark (`#16151A`), never pure black.** Modality hex values carry over without adjustment.
- **No purple/violet gradients. No teal SaaS blue. No three-color CTA gradients.** Anywhere.

## Spacing

- **Base unit:** 4px.
- **Density:** Comfortable for hero/prose, compact for data tables (BD readers skim tables).
- **Scale:** `2 / 4 / 8 / 16 / 24 / 32 / 48 / 64 / 96` (2xs / xs / sm / md / lg / xl / 2xl / 3xl / 4xl).
- **Vertical rhythm:** generous above the fold (cross-cut grid breathes), denser below.

## Layout

- **Approach:** Hybrid. Cross-cut grid full content width, prose narrower, tables full-bleed.
- **Max content width:** 1280px (cross-cut grid, tables).
- **Max prose width:** 720px (synthesis paragraph, editorial sections).
- **Border-radius:** **2px on cards, 0 on inputs / search.** NOT bubbly SaaS rounded. Editorial, not consumer-app.
- **Borders:** 1px hairline `#E8E5DD`. No shadows except a very subtle `box-shadow: 0 1px 0 rgba(0,0,0,0.02)` on the sticky header.
- **No zebra striping on tables.** Hairline border between rows is enough.
- **Sticky header** with antigen name + alias chips when scrolling past the hero.

### Page anatomy (antigen scorecard)

1. Slim sticky header: wordmark / search / "as of YYYY-MM-DD" / theme toggle
2. Hero (full content width, 1280px max):
   - Eyebrow ("TUMOUR-ASSOCIATED ANTIGEN")
   - Antigen name (Source Serif 4, 80px)
   - Alias chips + indications meta line
   - **Cross-cut grid** (the visual hero; one row per modality, bar length proportional to program count, count + phase pill at right)
3. Two-column section: 2fr/1fr — left is the AI synthesis paragraph (with inline `[N]` citation marks); right is a Quick Stats card
4. Per-modality detail tables (one block per modality, ADC first / most active)
5. Citations footer (numbered list, 2 columns, JetBrains Mono for IDs)

## Motion

- **Approach:** Minimal-functional. Page is for reading; animation is distraction.
- **Easing:** `ease-out` on hover transitions.
- **Duration:** 150ms on link/border hover; 100ms on button press.
- **No scroll-driven animation. No entrance animation. No fades on data load.** Stale-data badges appear without transition.

## Anti-slop directives

The following are forbidden in TAA Tracker UI:

- Purple / violet gradients
- 3-column feature grid with icons in colored circles
- Centered everything with uniform spacing
- Gradient buttons as the primary CTA pattern
- Generic stock-photo hero sections
- `system-ui` / `-apple-system` as primary display or body font
- Inter, Roboto, Helvetica, Open Sans, Montserrat, Poppins, Space Grotesk as primary fonts
- Bubble-radius border-radius (≥ 8px) on cards, tables, or inputs
- Bouncy entrance animations
- Marketing-copy patterns ("Built for X", "Designed for Y") on scorecard pages

## Decisions Log

| Date       | Decision                                              | Rationale |
|------------|-------------------------------------------------------|-----------|
| 2026-05-02 | Created design system via /design-consultation        | Anchors the modality-cross-cut moment; departures from Open Targets / HPA category conventions are deliberate. |
| 2026-05-02 | Display font: Source Serif 4 (replacing Fraunces)     | Fraunces was too busy at display sizes for BD readers. Source Serif 4 retains publication feel, reads cleaner, familiar to pharma audience. |
| 2026-05-02 | Cross-cut grid above all data tables (inverted layout)| Eureka from research: every biotech reference site buries data viz under tables. BD audience wants gestalt first, drill-down second. Inversion is the design wedge. |
| 2026-05-02 | Warm off-white background (`#FAFAF7`), not pure white | Signals publication, not app. Reduces fatigue on dense scorecard reading. |
| 2026-05-02 | Modality colors are muted (NYT-election-map palette)  | Distinguishable but adult. Bright SaaS palettes would read as toy. Used ONLY in the cross-cut grid + per-section markers. |
| 2026-05-02 | 2px border-radius on cards, 0 on inputs               | Editorial, not consumer-app. Modern bubble-radius would fight the publication framing. |
