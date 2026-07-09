<script>
  // Colored, clickable alert chip with a count badge.
  let { tone = 'info', label = '', count = null, icon, onclick } = $props();

  // tone -> token-driven colors via inline style (color-mix for soft bg/border)
  const tones = {
    danger: 'var(--color-danger)',
    warning: 'var(--color-warning)',
    info: 'var(--color-info)',
    success: 'var(--color-success)'
  };
  let c = $derived(tones[tone] ?? tones.info);
</script>

<button
  {onclick}
  class="flex items-center gap-2.5 rounded-[11px] border px-3.5 py-2.5 text-[13px] font-semibold transition-transform duration-150 hover:-translate-y-0.5 hover:shadow-[var(--shadow-card)] focus-visible:outline-2"
  style="color:{c}; background:color-mix(in srgb, {c} 12%, var(--color-surface)); border-color:color-mix(in srgb, {c} 35%, transparent)"
>
  {#if icon}{@const Icon = icon}<Icon size={16} />{/if}
  <span>{label}</span>
  {#if count !== null}
    <span
      class="rounded-lg px-2 py-0.5 text-[11px] font-bold"
      style="background:color-mix(in srgb, {c} 22%, transparent)">{count}</span
    >
  {/if}
</button>
