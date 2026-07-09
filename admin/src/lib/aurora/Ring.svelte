<script>
  // Circular progress metric card (ARIA-style donut).
  let {
    value = 0, // 0..100
    label = '',
    sub = '',
    color = 'var(--color-accent)',
    suffix = '%',
    display = null // override center text
  } = $props();

  const R = 34;
  const C = 2 * Math.PI * R;
  let pct = $derived(Math.max(0, Math.min(1, (Number(value) || 0) / 100)));
  let offset = $derived(C * (1 - pct));
  let center = $derived(display ?? `${Math.round(Number(value) || 0)}${suffix}`);
</script>

<div
  class="elev flex flex-col items-center rounded-2xl border-[0.5px] border-line bg-surface p-[18px] transition-transform duration-150 hover:-translate-y-0.5"
>
  <div class="relative h-[84px] w-[84px]">
    <svg width="84" height="84" class="-rotate-90">
      <circle cx="42" cy="42" r={R} fill="none" stroke="var(--color-surface-2)" stroke-width="8" />
      <circle
        cx="42"
        cy="42"
        r={R}
        fill="none"
        stroke={color}
        stroke-width="8"
        stroke-linecap="round"
        stroke-dasharray={C}
        stroke-dashoffset={offset}
        style="transition: stroke-dashoffset .6s ease"
      />
    </svg>
    <div class="absolute inset-0 flex flex-col items-center justify-center">
      <span class="text-[22px] font-bold tnum text-ink">{center}</span>
    </div>
  </div>
  <div class="mt-3 text-[13px] font-semibold text-ink">{label}</div>
  {#if sub}<div class="mt-0.5 text-center text-[11.5px] text-ink-3">{sub}</div>{/if}
</div>
