<script>
  // A donut is only defensible below about three slices — beyond that a sorted
  // bar chart communicates strictly more, so `RankBars` is used instead. This
  // one shows where answers come from: fast_path, agent, and the turns that
  // predate path instrumentation, which keep their own slice and are never
  // folded into a bucket that would flatter the chart.
  import { donutArc } from '$lib/charts/geom.js';
  import { isNum } from './format.js';

  let { slices = [], onpick = null } = $props();

  // Adjacent slices are separated by a surface-coloured stroke rather than by
  // their own contrast, for the reason spelled out in StackedBars.svelte: WCAG
  // 3:1 is a luminance ratio and no palette of more than three colours can hold
  // it pairwise. Every series fill clears 3:1 against --color-surface, so a
  // surface-coloured boundary is >= 3:1 against both slices that meet at it.
  //
  // Same width discipline as the bars, and the same reason. The ring is
  // r_inner 44 in a 132-unit box (geom.js `donutArc`), so a slice's narrowest
  // edge is its inner arc: 2*PI*44 = 276.5 units for the whole circle, i.e.
  // 2.77 units per percent. A 1px stroke straddling both radial edges of a
  // slice costs it 1 unit of arc, so a slice below MIN_ARC is left unstroked
  // rather than half-erased. At that size — under 1.1% of the total — the slice
  // is not identifiable by colour anyway; its <title> and the legend row beside
  // it are how it is read.
  const SEPARATOR = 'var(--color-surface)';
  const SEPARATOR_WIDTH = 1;
  const MIN_ARC = 3 / (2 * Math.PI * 44); // 3 units of inner arc

  let total = $derived(slices.reduce((a, s) => a + (isNum(s.value) ? s.value : 0), 0));
  let arcs = $derived.by(() => {
    if (total <= 0) return [];
    let acc = 0;
    return slices
      .filter((s) => isNum(s.value) && s.value > 0)
      .map((s) => {
        const start = acc / total;
        acc += s.value;
        const frac = acc / total - start;
        return { ...s, frac, sep: frac >= MIN_ARC, d: donutArc(start, acc / total) };
      });
  });
</script>

{#if total <= 0}
  <p class="rounded-card border border-dashed border-line-2 bg-surface px-4 py-6 text-center text-body-sm text-ink-3">
    Nothing recorded in this range.
  </p>
{:else}
  <div class="flex flex-wrap items-center gap-6 rounded-card border border-line bg-surface p-4">
    <svg viewBox="0 0 132 132" class="h-[132px] w-[132px] flex-none" role="img" aria-label="Answer source composition">
      {#each arcs as a (a.key)}
        <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
        <path
          d={a.d}
          fill={a.color}
          stroke={a.sep ? SEPARATOR : 'none'}
          stroke-width={a.sep ? SEPARATOR_WIDTH : 0}
          class={onpick ? 'cursor-pointer hover:opacity-85' : ''}
          role={onpick ? 'button' : undefined}
          tabindex={onpick ? 0 : undefined}
          aria-label={onpick ? `Filter to ${a.label}` : undefined}
          onclick={onpick ? () => onpick(a) : undefined}
          onkeydown={onpick ? (e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), onpick(a)) : undefined}
        >
          <title>{a.label}: {a.value}</title>
        </path>
      {/each}
    </svg>
    <div class="min-w-[190px]">
      {#each slices as s (s.key)}
        <svelte:element
          this={onpick && s.pickable !== false ? 'button' : 'div'}
          type={onpick && s.pickable !== false ? 'button' : undefined}
          role={onpick && s.pickable !== false ? 'button' : undefined}
          onclick={onpick && s.pickable !== false ? () => onpick(s) : undefined}
          class="flex w-full items-center gap-2 rounded-control px-1.5 py-1 text-body-sm text-ink-2
                 {onpick && s.pickable !== false ? 'cursor-pointer hover:bg-accent-soft' : ''}"
        >
          <i class="h-2.5 w-2.5 flex-none rounded-xs" style="background:{s.color}"></i>
          <span class="truncate">{s.label}</span>
          <b class="ml-auto text-ink tnum">{isNum(s.value) ? s.value.toLocaleString() : '—'}</b>
        </svelte:element>
      {/each}
    </div>
  </div>
{/if}
