<script>
  // Two categorical axes + magnitude → grid, colour = intensity.
  //
  // The ramp is SINGLE-HUE. A rainbow ramp implies category boundaries between
  // the colours, and a magnitude has none — the reader starts believing green
  // and blue mean different kinds of thing rather than more and less.
  //
  // An unknown cell is drawn empty, not at the coldest colour: the coldest
  // colour is a measured zero, and cohort retention with no user identity is
  // unknown rather than zero.
  import { heatStyle } from '$lib/charts/geom.js';
  import { isNum, UNKNOWN } from './format.js';

  let { cols = [], rows = [], onpick = null, cellLabel = (v) => String(v) } = $props();

  let max = $derived(
    Math.max(
      1,
      ...rows.flatMap((r) => r.cells.map((c) => (isNum(c.value) ? c.value : 0)))
    )
  );
</script>

<div class="overflow-x-auto">
  <table class="border-separate border-spacing-[3px] rounded-card border border-line bg-surface p-3">
    <thead>
      <tr>
        <th></th>
        {#each cols as c (c.key)}
          <th class="px-2 py-1 text-center text-label font-semibold text-ink-3">{c.label}</th>
        {/each}
      </tr>
    </thead>
    <tbody>
      {#each rows as r (r.key)}
        <tr>
          <th class="px-2 py-1 text-right text-label font-semibold whitespace-nowrap text-ink-3">{r.label}</th>
          {#each r.cells as c, ci (cols[ci].key)}
            {@const st = heatStyle(c.value, max)}
            <td
              class="h-9 w-[74px] rounded-control text-center text-meta font-semibold tnum
                     {onpick && st.known ? 'cursor-pointer hover:outline hover:outline-2 hover:outline-accent' : ''}"
              style="background:{st.background};color:{st.color}"
              role={onpick && st.known ? 'button' : undefined}
              tabindex={onpick && st.known ? 0 : undefined}
              aria-label={onpick && st.known ? `${r.label} × ${cols[ci].label}: ${c.value}` : `${r.label} × ${cols[ci].label}: not recorded`}
              onclick={onpick && st.known ? () => onpick(r, cols[ci], c) : undefined}
              onkeydown={onpick && st.known ? (e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), onpick(r, cols[ci], c)) : undefined}
              title="{r.label} × {cols[ci].label}: {isNum(c.value) ? c.value : UNKNOWN}"
            >
              {isNum(c.value) ? cellLabel(c.value) : ''}
            </td>
          {/each}
        </tr>
      {/each}
    </tbody>
  </table>
</div>
