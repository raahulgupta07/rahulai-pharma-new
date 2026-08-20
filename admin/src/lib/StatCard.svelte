<script>
  let { label = '', value = '', unit = '', delta = '', deltaTone = 'neutral' } = $props();
  const dmap = { up: 'text-success', down: 'text-danger', neutral: 'text-ink-3' };
</script>

<div class="elev rounded-card border border-line bg-surface p-4">
  <div class="mb-1.5 text-body-sm font-medium text-ink-2">{label}</div>
  <div class="flex items-baseline gap-1.5">
    <span class="text-display font-bold tnum text-ink">{value}</span>
    {#if unit}<span class="text-meta text-ink-3">{unit}</span>{/if}
    {#if delta}<span class="text-meta {dmap[deltaTone] ?? dmap.neutral}">{delta}</span>{/if}
  </div>
</div>
