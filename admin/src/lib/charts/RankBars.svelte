<script>
  // Ranked categories → horizontal bars. Long labels (a Burmese question, a
  // model id like google/gemini-3.5-flash, a store code) stay readable on their
  // own line, which is the whole reason this is not a column chart. Sorted bars
  // beat a pie, always, and there is no pie here beyond a 3-slice donut.
  //
  // Each row is a button and drills through to the turns behind it.
  import { isNum, UNKNOWN } from './format.js';

  let { rows = [], onpick = null, empty = 'Nothing recorded in this range.' } = $props();

  const TONE = {
    accent: 'bg-accent',
    ok: 'bg-success',
    bad: 'bg-danger',
    warn: 'bg-warning',
    muted: 'bg-accent-2'
  };

  // Widths are relative to the largest KNOWN value. A row whose value is
  // unknown gets no bar at all rather than a zero-width one, which would read
  // as a measured zero.
  let max = $derived(Math.max(1, ...rows.map((r) => (isNum(r.value) ? r.value : 0))));
</script>

{#if rows.length === 0}
  <p class="rounded-card border border-dashed border-line-2 bg-surface px-4 py-6 text-center text-body-sm text-ink-3">
    {empty}
  </p>
{:else}
  <div class="rounded-card border border-line bg-surface px-2 py-1.5">
    {#each rows as r (r.key)}
      <svelte:element
        this={onpick ? 'button' : 'div'}
        type={onpick ? 'button' : undefined}
        role={onpick ? 'button' : undefined}
        onclick={onpick ? () => onpick(r) : undefined}
        class="grid w-full grid-cols-[minmax(120px,190px)_1fr_86px] items-center gap-3 rounded-panel px-2 py-1.5 text-left
               {onpick ? 'cursor-pointer hover:bg-accent-soft' : ''}"
        style="min-height:38px"
        aria-label={onpick ? `Filter by ${r.label}` : undefined}
      >
        <span class="truncate text-meta text-ink-2" title={r.sub ? `${r.label} — ${r.sub}` : r.label}>
          {r.label}{#if r.sub}<i class="ml-1.5 text-label font-normal text-ink-3 not-italic">{r.sub}</i>{/if}
        </span>
        <span class="h-2 overflow-hidden rounded bg-surface-2">
          {#if isNum(r.value)}
            <span
              class="block h-full rounded {TONE[r.tone] ?? TONE.accent}"
              style="width:{Math.max(r.value > 0 ? 1.5 : 0, (r.value / max) * 100).toFixed(1)}%"
            ></span>
          {/if}
        </span>
        <span class="text-right text-meta font-semibold text-ink-2 tnum">
          {r.valueLabel ?? (isNum(r.value) ? r.value.toLocaleString() : UNKNOWN)}
        </span>
      </svelte:element>
    {/each}
  </div>
{/if}
