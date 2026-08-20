# Console design system

What this console looks like, and why each value is the value it is. Written
during the agentdash restyle (2026-08-18) so Phase 1 is a mechanical edit of
`src/app.css` rather than a series of judgement calls made twice.

The reference is `CityAgentWork/agentdash` — the CityAgent Insights frontend
(Nuxt 3 + Nuxt UI + Tailwind). Its design was measured, not eyeballed: token
frequencies below come from counting every `class="…"` in its `pages/`,
`components/` and `layouts/` (135,591 lines).

**This console already has a token layer.** `:root` publishes `--c-*`, `@theme`
maps them onto Tailwind utility names, and pages consume `bg-surface`,
`text-ink-2`, `border-line`. Changing a token changes every page. That is why
the restyle is 148 lines of CSS and not 10,516 lines of markup.

---

## 1. What was measured in agentdash

| axis | what dominates | count |
|---|---|---|
| body text | `text-xs` (12px) | 2077 |
| secondary | `text-sm` (14px) | 1101 |
| meta / labels | `text-[11px]` | 904 |
| chips | `text-[10px]` | 597 |
| page title | `text-lg` (18px) | 125 |
| KPI number | `text-2xl` (24px) | 28 |
| weight | `font-medium` | 1254 |
| headings | `font-semibold` | 327 |
| neutral family | **gray** (not slate/zinc) | — |
| most-used text colour | `text-gray-400` | 1652 |
| surfaces | `bg-white` on `bg-gray-50` | 463 / 250 |
| borders | `border-gray-200` / `-100` | 641 / 295 |
| radius, controls | `rounded` · `-md` · `-lg` | 935 / 506 / 494 |
| radius, cards | `rounded-xl` / `-2xl` | 79 / 18 |
| elevation | `shadow-sm` only | 77 |
| accent | `blue-500` / `blue-600` | — |

Structural patterns worth copying verbatim:

- **Page**: `max-w-7xl px-4 py-4` → `h1` 18px/600 → underline tab strip → content.
- **Tab strip**: `border-b border-gray-200`, `-mb-px flex gap-x-6`, tabs
  `border-b-2 py-4 px-1 text-sm font-medium`; active
  `border-blue-500 text-blue-600`, idle `border-transparent text-gray-500`.
- **Card**: `bg-white p-6 border border-gray-200 rounded-xl shadow-sm`.
- **Panel**: `rounded-2xl border border-gray-100 shadow-sm overflow-hidden`,
  header `p-6 border-b border-gray-50`.
- **Segmented control**: `inline-flex rounded-md border border-gray-200
  overflow-hidden`, buttons `px-3 py-1 text-xs font-medium border-s`.

---

## 2. Three places we deliberately do NOT copy agentdash

These are decisions, not oversights. Re-deriving them later wastes a day.

### 2.1 `text-gray-400` fails AA and we will not ship it

agentdash's single most-used text colour is `text-gray-400` `#9CA3AF`.
Measured against its own `bg-white`: **2.54:1**. Against `bg-gray-50`: **2.43:1**.
WCAG AA for body text is 4.5:1.

This console's current `--c-ink-3` is `oklch(58% 0.012 240)` = `#747b81` =
**4.29:1**. Adopting agentdash's literal value would take secondary text from
near-AA to less than half of AA, in a console that pharmacy staff read stock
numbers off.

**Decision: `--c-ink-3` becomes gray-500 `#6B7280` (4.83:1), not gray-400.**
Everything else about the neutral ramp follows agentdash. The look is
indistinguishable at a glance; the text stays readable.

### 2.2 The Burmese face stays

agentdash loads **no webfont at all** — its `font-family:'Inter',system-ui,…`
on the sign-in page names a font it never fetches, so it renders system-ui.
"Matching its font" therefore means matching the *system stack*, not adding Inter.

But this console answers in Burmese. `Noto Sans Myanmar` is load-bearing and
stays in `--font-sans` after the system stack. Dropping it to match agentdash
literally would break every Burmese answer, the chat tester, and the catalog.

`JetBrains Mono` stays for `--font-mono` (SKU codes, cache keys, file names).
`Nunito` (`--font-display`) **goes** — agentdash has no display face; `.page-title`
falls back to `--font-sans` at the same weight.

### 2.3 The logo cyan loses its second job

`--c-accent-2` `#00ADEF` is the CityCare logo's cyan. In charts it is the
second data series. agentdash's second series is `gray-300`.

`#00ADEF` on white is **2.55:1**, so it already may not carry text — that rule
predates this restyle and is documented in `app.css`. After the restyle it is
retained as a **brand mark only** and charts use gray-300 for the comparison
series, matching agentdash. If a chart currently relies on cyan to distinguish
two live series, that chart needs a real second accent, not a brand colour.

---

### 2.4 The sign-in showcase is the one surface that stays dark in both themes

agentdash's own sign-in page puts a fixed navy panel beside the form
(`AuthShowcase.vue`: `radial-gradient(120% 120% at 82% -12%, #1e3a8a, #0f1e3d,
#0a1226)`), and this console's equivalent panel is a product demo rather than a
page. It does not follow the theme, so it cannot read from `--c-*` tokens that
flip.

It used to. The panel was `bg-[#141110]` warm-black with `text-accent`,
`bg-accent` and `border-accent/50` inside it. In **light** mode `--c-accent` is
blue-600 `#2563EB`, which measures **3.0:1** on that background — every
accent-coloured word in the demo was below AA for half the console's users, and
nothing in the page said so because the same markup passes in dark mode.

**Decision: a separate `--c-show-*` scale, defined in `:root` only and inherited
unchanged by `html.dark`.** `--c-show-accent` is blue-400 `#60A5FA` (**7.0:1**
on `--c-show-bg`). Inside the showcase, never reach for `--c-accent`.

| token | value | role |
|---|---|---|
| `--c-show-bg` / `-2` / `-3` | `#0A1226` / `#0F1E3D` / `#1E3A8A` | the three radial stops |
| `--c-show-ink` / `-2` / `-3` | `#EEF4FF` / `#C7D6EF` / `#8BA0C4` | title / body / meta |
| `--c-show-accent` | `#60A5FA` | active step, live values |
| `--c-show-success` | `#6EE7A8` | completed step, "SSO ready" |
| `--c-show-line` / `-2` | `rgba(255,255,255,.09)` / `rgba(96,165,250,.28)` | inner rules / panel edge |

The gradient itself lives in `app.css` as `.showcase-panel`, because a gradient
cannot be a colour token and Rule 1 forbids putting the literal in the page.

---

## 3. Token map — old → new

Every `--c-*` below already exists. Phase 1 changes values only; no token is
added or removed, so no page can break from a missing name.

### Light (`:root`)

| token | today | after | contrast |
|---|---|---|---|
| `--c-page` | `oklch(98% .004 240)` `#f6f9fb` | `#F9FAFB` gray-50 | — |
| `--c-surface` | `#ffffff` | `#ffffff` unchanged | — |
| `--c-surface-2` | `oklch(96% .006 240)` | `#F3F4F6` gray-100 | — |
| `--c-ink` | `#0e151a` 18.41 | `#111827` gray-900 | **17.74** |
| `--c-ink-2` | `#464e54` 8.47 | `#4B5563` gray-600 | **7.56** |
| `--c-ink-3` | `#747b81` 4.29 | `#6B7280` gray-500 | **4.83** ↑ |
| `--c-accent` | `#2F3293` indigo | `#2563EB` blue-600 | **5.17** |
| `--c-accent-hover` | `#262a7d` | `#1D4ED8` blue-700 | — |
| `--c-accent-soft` | `#eaebf7` | `#EFF6FF` blue-50 | 6.16 w/ blue-700 |
| `--c-accent-2` | `#00ADEF` | `#D1D5DB` gray-300 (charts) | mark only |
| `--c-accent-2-soft` | `#e3f5fd` | `#F3F4F6` | — |
| `--c-on-accent` | `#ffffff` | `#ffffff` | **5.17** on accent |
| `--c-line` | `oklch(88% …)` | `#E5E7EB` gray-200 | — |
| `--c-line-2` | `oklch(93% …)` | `#F3F4F6` gray-100 | — |
| `--c-success` | `#298646` 4.57 | `#047857` emerald-700 | **5.48** ↑ |
| `--c-success-soft` | `oklch(94% …)` | `#ECFDF5` | — |
| `--c-warning` | `#be8200` **3.29 FAIL** | `#B45309` amber-700 | **5.02** ↑↑ |
| `--c-warning-soft` | `oklch(95% …)` | `#FFFBEB` | — |
| `--c-danger` | `#c53637` 5.31 | `#DC2626` red-600 | **4.83** |
| `--c-danger-soft` | `oklch(95% …)` | `#FEF2F2` | — |
| `--c-info` | `oklch(52% .10 230)` | `#2563EB` blue-600 | **5.17** |
| `--c-info-soft` | `oklch(94% …)` | `#EFF6FF` | — |

Net accessibility effect: **ink-3 4.29→4.83, success 4.57→5.48, warning
3.29→5.02.** Danger drops 5.31→4.83, still clear of 4.5. No pair regresses
below AA. Emerald-600 (`#059669`, 3.77) and amber-600 (`#D97706`, 3.19) are the
values Tailwind dashboards usually reach for and are **rejected here** — both
are large-text-only.

### Dark (`html.dark`)

| token | after | contrast on surface |
|---|---|---|
| `--c-page` | `#030712` gray-950 | — |
| `--c-surface` | `#111827` gray-900 | — |
| `--c-surface-2` | `#1F2937` gray-800 | — |
| `--c-ink` | `#F3F4F6` | **16.12** |
| `--c-ink-2` | `#9CA3AF` | **6.99** |
| `--c-ink-3` | `#6B7280` | 3.67 — meta/label only, never body |
| `--c-accent` | `#60A5FA` blue-400 | **6.98** |
| `--c-accent-hover` | `#93C5FD` | — |
| `--c-accent-soft` | `#1E3A8A` | — |
| `--c-on-accent` | `#0B1020` | **7.45** on accent |
| `--c-line` / `-2` | `#374151` / `#1F2937` | — |
| `--c-success` | `#34D399` | **9.23** |
| `--c-danger` | `#F87171` | **6.41** |

The existing rule survives the restyle: **`--c-on-accent` flips with the mode.**
Light accent is dark blue → white text; dark accent is light blue → near-black
text. Never white on the dark-mode accent.

### Shape and elevation

| token | today | after |
|---|---|---|
| `--shadow-card` | `0 1px 2px …04, 0 4px 16px …05` | `0 1px 2px rgba(0,0,0,.05)` |
| `--shadow-pop` | `0 16px 40px …15` | `0 10px 15px -3px rgba(0,0,0,.1), 0 4px 6px -4px rgba(0,0,0,.1)` |
| *(new)* `--radius-card` | — | `12px` (`rounded-xl`) |
| *(new)* `--radius-panel` | — | `16px` (`rounded-2xl`) |
| *(new)* `--radius-control` | — | `6px` (`rounded-md`) |

---

## 4. Component contract

What each primitive must look like after Phase 3. One row = one file.

| component | spec |
|---|---|
| `PageHeader` | `h1` 18px/600 `text-ink`; sub 13px `text-ink-3`; right-hand action slot |
| `TabStrip` *(new)* | `border-b border-line`; `-mb-px flex gap-x-6`; tab `border-b-2 py-4 px-1 text-sm font-medium`; active `border-accent text-accent` |
| `charts/Kpi` | card; value 24px/700 `text-ink`; label 14px/500 `text-ink-2`; sub 12px `text-ink-3` |
| `charts/Section` | panel; header `p-6 border-b border-line-2`; `h3` 18px/600 |
| `charts/Table` | 12px rows; `th` 11px uppercase `.05em` `text-ink-3`; row border `line-2`; hover `surface-2` |
| `charts/RankBars` | 8px track `surface-2`, 4px radius, fill `accent`, muted fill `accent-2` |
| `StatusPill` / `Badge` | `rounded-full`, 11px/500, soft bg + matching ink |
| `Toggle` / `SettingRow` | 44px min touch target retained |
| `ErrorState` / `Stub` | `danger-soft` panel, 14px/700 title, 12.5px body |

### Two rows of that table were not built as written (Phase 3)

Both are recorded here rather than silently diverging, because a spec that
disagrees with the code is worse than either one alone.

- **`ErrorState` is not a flat `danger-soft` panel.** It is `danger-soft` only
  for a 5xx or a dead connection, and `warning-soft` for 401/403/404. Rule 3
  below is why: a refusal is not a failure. An expired session, a role that does
  not reach a page, and a backend older than this console are all facts about
  who you are or what is deployed — painting them red asserts the server broke,
  which the reader then has to un-learn.
- **`charts/Section` did not become a bordered panel**, only its heading type
  changed (18px/600). Its children already render their own bordered cards, so
  wrapping the section would draw a border around a border on every analytics
  tab. Making Section a real panel means removing the inner card from each of
  its callers, which is page work, not primitive work.

Chart series colours live in **one** place (`C.accent`, `C.a2` in
`analytics/+page.svelte`) and must read from the tokens, not literals.

---

## 5. Rules that outlive this restyle

1. **Never introduce a colour as a literal hex in a page.** It will not follow
   dark mode. Use a token.
2. **A number that was not recorded shows as an em-dash, never as zero.** This
   predates the restyle and the visual language must keep supporting it —
   em-dash is `text-ink-3`.
3. **Refusal is not failure.** Diagnostics colours these separately: refused =
   warning, failed = danger. Do not merge them into one "error" tone.
4. **44px minimum touch target under `@media (pointer: coarse)`** stays.
5. **`prefers-reduced-motion` kill-switch** stays.
6. Focus ring is `2px solid var(--color-accent)` with `outline-offset: 2px` on
   every focusable element. Changing the accent changes the ring — verify it is
   visible on both `surface` and `accent` backgrounds after Phase 1.
