<script>
  // A tab-level caveat that has to be read before the numbers under it — how
  // thin the sample is, or which column the whole tab depends on. It sits above
  // the KPI row rather than under the charts, because a caveat below the fold
  // is a caveat nobody reads.
  //
  // `tone="ok"` is for the same kind of statement in the other direction: "these
  // numbers ARE measured, and here is what makes them so". It reads as a caveat
  // bar on purpose — a reader who has learnt to check this strip should find the
  // good news in the same place, not have to infer it from the absence of a
  // warning.
  let { children, tone = 'warn' } = $props();

  const HUE = { warn: 'var(--color-warning)', ok: 'var(--color-success)', bad: 'var(--color-danger)' };
  let hue = $derived(HUE[tone] ?? HUE.warn);
</script>

<div
  class="mb-4 rounded-card border px-3.5 py-3 text-body-sm leading-relaxed text-ink"
  style="background:color-mix(in srgb, {hue} 12%, transparent);
         border-color:color-mix(in srgb, {hue} 40%, transparent)"
>
  {@render children()}
</div>
