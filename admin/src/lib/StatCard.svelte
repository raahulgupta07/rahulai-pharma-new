<script>
  let { label = '', value = '', unit = '', delta = '', deltaTone = 'neutral' } = $props();
  const dmap = { up: 'text-success', down: 'text-danger', neutral: 'text-ink-3' };
</script>

<div class="elev rounded-2xl border-[0.5px] border-line bg-surface p-4 transition-transform duration-150 hover:-translate-y-0.5">
  <div class="mb-1.5 text-[12px] text-ink-2">{label}</div>
  <div class="flex items-baseline gap-1.5">
    <span class="text-[24px] font-medium tnum">{value}</span>
    {#if unit}<span class="text-[12px] text-ink-3">{unit}</span>{/if}
    {#if delta}<span class="text-[12px] {dmap[deltaTone] ?? dmap.neutral}">{delta}</span>{/if}
  </div>
</div>
