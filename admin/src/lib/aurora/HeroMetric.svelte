<script>
  // Hero metric card: big number + inline bar sparkline + side stats.
  let {
    value = '—',
    suffix = '',
    caption = '',
    live = '',
    bars = [], // array of 0..100 heights
    stats = [] // [{ value, label }]
  } = $props();
</script>

<div
  class="elev grid grid-cols-1 items-center gap-6 rounded-2xl border-[0.5px] border-line bg-surface p-6 lg:grid-cols-[1.1fr_1.5fr_0.8fr]"
>
  <div>
    <div class="text-[52px] font-bold leading-none tracking-tight tnum text-ink">
      {value}<span class="text-[24px] text-ink-3">{suffix}</span>
    </div>
    {#if caption}<div class="mt-2 max-w-[210px] text-[13.5px] text-ink-2">{caption}</div>{/if}
    {#if live}
      <div class="mt-2 flex items-center gap-2 text-[12.5px] font-semibold text-success">
        <span class="h-2 w-2 rounded-full bg-success"></span>{live}
      </div>
    {/if}
  </div>

  <div class="flex h-[78px] items-end gap-[5px]">
    {#each bars as h}
      <div
        class="flex-1 rounded-t-[5px] transition-transform hover:scale-y-[1.04]"
        style="height:{Math.max(5, h)}%; background:linear-gradient(180deg,var(--color-accent),var(--color-accent-hover)); opacity:.9"
      ></div>
    {/each}
  </div>

  <div>
    {#each stats as s}
      <div class="py-1.5 text-right">
        <div class="text-[21px] font-bold tnum text-ink">{s.value}</div>
        <div class="mt-0.5 text-[11.5px] text-ink-3">{s.label}</div>
      </div>
    {/each}
  </div>
</div>
