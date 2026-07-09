<script>
  let { tone = 'neutral', children } = $props();
  const map = {
    en: 'bg-info-soft text-info',
    my: 'bg-accent-soft text-accent-hover',
    ok: 'bg-success-soft text-success',
    warn: 'bg-warning-soft text-warning',
    danger: 'bg-danger-soft text-danger',
    info: 'bg-info-soft text-info',
    neutral: 'bg-surface-2 text-ink-2'
  };
</script>

<span class="inline-flex items-center rounded-lg px-2 py-0.5 text-[11px] font-medium {map[tone] ?? map.neutral}">
  {@render children?.()}
</span>
