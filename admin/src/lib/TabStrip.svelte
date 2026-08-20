<!--
  The underline tab strip, once.

  Seven pages hand-rolled this before (analytics, ftp, embed, quality, settings,
  AuthPanel, BrandingPanel) and five of them were byte-identical apart from the
  tab list. That duplication was not only markup: each copy carried its own
  roving-tabindex and arrow-key handler, so `ftp` — the one copy that omitted
  them — was the one page where the tabs could not be driven from the keyboard.
  A primitive is how that stops being per-page luck.

  Shape is agentdash's, measured: `border-b border-line`, `-mb-px` so the active
  underline sits ON the container's border rather than below it, tabs
  `border-b-2 py-4 px-1 text-sm font-medium`, active `border-accent text-accent`,
  idle `border-transparent text-ink-3`.

  Two deliberate departures, both keeping behaviour this console already had:
    * `min-h-[44px]` stays — the coarse-pointer rule in app.css applies to
      `button`, and these are buttons, but the padding is stated here so a tab
      is a legal touch target whether or not that media query matches.
    * `gap-x-6` is agentdash's spacing for ~5 tabs. Analytics has TEN, which
      overflows a 1440px window at that gap, so the strip scrolls horizontally
      (`overflow-x-auto`) and the caller may narrow the gap. Nothing is hidden.

  Keyboard: Left/Right move, Home/End jump, and focus follows selection — the
  WAI-ARIA "automatic activation" pattern, which is what the copies did.
-->
<script>
  let {
    /** [{ id, label, icon? }] — `icon` is a component, not a name. */
    tabs = [],
    /**
     * Selected id. Bindable, but most callers must NOT bind: on every page
     * whose tab lives in the query string, `tab` is `$derived` from the URL and
     * a derived cannot be bound. Those pass `value={tab} onchange={setTab}` and
     * the URL remains the single source of truth — the local assignment below
     * only paints the new tab immediately and is overwritten by the prop the
     * moment the navigation lands.
     */
    value = $bindable(''),
    /** Called with the new id. The caller usually syncs the query string here. */
    onchange = null,
    /** Tailwind gap between tabs. Narrow it when a strip has many tabs. */
    gap = 'gap-x-6',
    /**
     * Wrap to a second row instead of scrolling sideways. Analytics has TEN
     * tabs and wrapped before this component existed; a horizontal scroller
     * would have put three of them behind an affordance nobody looks for.
     */
    wrap = false,
    /**
     * Pin the strip while the panel scrolls under it. Four pages did this.
     *
     * The sticky wrapper pulls itself up over `main`'s 24px top padding with
     * `-mt-6` (see the note below the script), which assumes the strip is the
     * FIRST thing in `main`. Put a page header above it and that -24px eats the
     * header's bottom margin and then its last line of text. A caller that
     * renders anything before a sticky strip must leave 24px of space for it —
     * `class="pb-6"` on the header's wrapper — and `tests/test_admin_tabbed_pages.py`
     * fails the build if it does not.
     */
    sticky = false,
    /** aria-label for the tablist. */
    label = 'Sections',
    /** Optional trailing content per tab (a count badge, say). */
    trailing
  } = $props();

  let els = $state([]);

  function pick(id) {
    if (id === value) return;
    value = id;
    onchange?.(id);
  }

  function onKey(e) {
    const i = tabs.findIndex((t) => t.id === value);
    if (i < 0) return;
    let n = null;
    if (e.key === 'ArrowRight') n = (i + 1) % tabs.length;
    else if (e.key === 'ArrowLeft') n = (i - 1 + tabs.length) % tabs.length;
    else if (e.key === 'Home') n = 0;
    else if (e.key === 'End') n = tabs.length - 1;
    if (n === null) return;
    e.preventDefault();
    pick(tabs[n].id);
    els[n]?.focus();
  }
</script>

<!--
  The spacing lives on a WRAPPER, not on the tablist, because a sticky element
  only paints its own box and margin is transparent.

  Sticky used to be `mb-5 … sticky top-0 bg-page` on the tablist itself, which
  left TWO uncovered bands for content to scroll visibly through: the 20px
  margin below it, and `main`'s own 24px top padding above it (top-0 pins to the
  scroll port, which starts inside that padding). On Settings you could watch a
  card's text slide through both gaps while the bar sat between them.

  So: pull the wrapper up over main's padding with -mt-6, put that padding back
  as pt-6, and turn the bottom margin into pb-5. The border stays on the tablist
  so the active tab's underline still meets the rail.

  `-top-6` is the part that is easy to get wrong. Sticky offsets resolve against
  the scroll container's PADDING box, and `main` is `py-6` — so `top-0` pins 24px
  BELOW the header and leaves that band permanently uncovered, which is where the
  bleed-through was still visible after the margin was fixed. Measured, not
  reasoned: main's padding box starts at y=84 while its border box starts at 60.
  Sticking at -24px clamps the wrapper's top to 60, and its own pt-6 puts the
  tabs back at 84 — same place they were, with every pixel above them painted.
-->
<div class={sticky ? 'sticky -top-6 z-30 -mt-6 bg-page pt-6 pb-5' : 'mb-5'}>
<div
  role="tablist"
  aria-label={label}
  onkeydown={onKey}
  tabindex="-1"
  class="flex border-b border-line {gap}
    {wrap ? 'flex-wrap' : 'overflow-x-auto'}"
>
  {#each tabs as t, i (t.id)}
    {@const on = t.id === value}
    <button
      bind:this={els[i]}
      role="tab"
      id={'tab-' + t.id}
      aria-selected={on}
      aria-controls={'panel-' + t.id}
      tabindex={on ? 0 : -1}
      onclick={() => pick(t.id)}
      class="-mb-px flex min-h-[44px] flex-none cursor-pointer items-center gap-1.5 border-b-2 px-1 py-4
             text-body-sm font-medium whitespace-nowrap transition-colors
             focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px]
             focus-visible:outline-accent
             {on ? 'border-accent text-accent' : 'border-transparent text-ink-3 hover:text-ink'}"
    >
      {#if t.icon}{@const Icon = t.icon}<Icon size={15} />{/if}
      {t.label}
      {#if trailing}{@render trailing(t)}{/if}
    </button>
  {/each}
</div>
</div>
