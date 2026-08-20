<script>
  // A KPI card has FOUR parts: label, number, sparkline, benchmark footnote.
  //
  // The fourth is the one everyone omits and the one that makes the number
  // actionable — "target >30% is healthy", "23 up · 2 down · only 25 of 125
  // turns rated". A number without an interpretation rule is a number nobody
  // acts on, so the footnote is required, not optional, and it lives on the
  // card rather than in a tooltip.
  //
  // `value === null` renders the em-dash, muted, with the sparkline slot held
  // open so a row of cards does not jump when one of them has no data.
  //
  // A fifth part is optional and, where the API supplies it, expected: the
  // movement chip. Its rules live in DeltaChip — the two that matter at this
  // call site are that a missing block draws nothing (unknown) while a block
  // with a null delta prints "no prior period", and that `good` decides the
  // colour, because "up" is an improvement for a hit rate and a regression for
  // a latency.
  import { sparkPath } from '$lib/charts/geom.js';
  import { UNKNOWN } from './format.js';
  import DeltaChip from './DeltaChip.svelte';

  let {
    label,
    value = null,
    unit = '',
    foot = '',
    spark = null,
    tone = 'accent',
    estimated = false,
    onclick = null,
    /** Movement block from the API, or null for "not supplied". */
    delta = null,
    /** Which direction is an improvement: 'up' | 'down' | 'none'. */
    good = 'up',
    /** Formats the absolute movement, e.g. seconds or dollars. */
    deltaFmt = undefined,
    /**
     * Set when this card's block reported `filters_applied: false`. The chip
     * goes on the CARD, not only in a banner at the top of the tab: a reader
     * looking at one number should not have to remember a warning they scrolled
     * past to know it ignores the chips above it.
     */
    unfiltered = false
  } = $props();

  const TONE = {
    accent: 'var(--color-accent)',
    ok: 'var(--color-success)',
    bad: 'var(--color-danger)',
    warn: 'var(--color-warning)',
    info: 'var(--color-series-2)'
  };
  let stroke = $derived(TONE[tone] ?? TONE.accent);
  let missing = $derived(value === null || value === undefined || value === UNKNOWN);
  let path = $derived(sparkPath(spark));
</script>

<svelte:element
  this={onclick ? 'button' : 'div'}
  type={onclick ? 'button' : undefined}
  role={onclick ? 'button' : undefined}
  onclick={onclick ?? undefined}
  class="elev block w-full rounded-card border border-line bg-surface px-4 py-3.5 text-left transition-colors
         {onclick ? 'cursor-pointer hover:border-accent hover:bg-accent-soft' : ''}"
>
  <div class="text-body-sm font-medium text-ink-2">{label}</div>

  <div class="mt-0.5 text-display leading-tight font-bold tracking-[-0.011em] tnum
              {missing ? 'text-ink-3' : 'text-ink'}">
    {missing ? UNKNOWN : value}{#if unit && !missing}<span class="ml-0.5 text-body-sm font-medium text-ink-2">{unit}</span>{/if}
  </div>

  <DeltaChip {delta} {good} fmt={deltaFmt} />

  {#if path}
    <svg class="mt-1.5 block h-[30px] w-full" viewBox="0 0 150 30" preserveAspectRatio="none" aria-hidden="true">
      <path d={path.area} fill={stroke} opacity="0.1" />
      <path
        d={path.line}
        fill="none"
        stroke={stroke}
        stroke-width="1.5"
        stroke-linejoin="round"
        stroke-linecap="round"
        vector-effect="non-scaling-stroke"
      />
    </svg>
  {:else}
    <!-- No series to draw. The slot is still reserved so cards stay aligned. -->
    <div class="mt-1.5 h-[30px]"></div>
  {/if}

  <div class="mt-1.5 text-meta leading-snug text-ink-3">
    {foot}{#if estimated}<span class="ml-1 rounded border border-warning px-1 py-px text-micro font-semibold text-warning">estimated</span>{/if}
    {#if unfiltered}
      <span
        class="ml-1 rounded border border-warning px-1 py-px text-micro font-semibold text-warning"
        title={typeof unfiltered === 'string' ? unfiltered : 'This number cannot honour the active filters.'}
      >unfiltered</span>
    {/if}
  </div>
</svelte:element>
