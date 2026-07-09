<script>
  // Hero metric panel: filled accent surface, big number, bar sparkline, side stats.
  // Everything on it uses text-on-accent, which flips in dark mode where the
  // accent itself becomes light.
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
  class="flex flex-wrap items-center gap-[26px] rounded-[18px] bg-accent p-[26px] text-on-accent"
>
  <div class="min-w-[180px]">
    <div class="page-title text-[44px] leading-none tnum">
      {value}<span class="text-[24px] opacity-70">{suffix}</span>
    </div>
    {#if caption}
      <div class="mt-1.5 max-w-[220px] text-[13px] opacity-85">{caption}</div>
    {/if}
    {#if live}
      <div class="mt-2.5 flex items-center gap-1.5 text-[11.5px] opacity-70">
        <span class="h-1.5 w-1.5 rounded-full bg-success-soft"></span>{live}
      </div>
    {/if}
  </div>

  <div class="flex h-[60px] min-w-[160px] flex-1 items-end gap-1">
    {#each bars as h}
      <div
        class="flex-1 rounded-t-[3px] bg-current transition-opacity hover:opacity-90"
        style="height:{Math.max(5, h)}%; opacity:.55"
      ></div>
    {/each}
  </div>

  <div class="flex flex-wrap gap-[22px]">
    {#each stats as s}
      <div>
        <div class="page-title text-[19px] tnum">{s.value}</div>
        <div class="text-[11px] opacity-80">{s.label}</div>
      </div>
    {/each}
  </div>
</div>
