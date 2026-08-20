<script>
  // Small status pill for the strip at the top of a page.
  let { value = '', label = '', dot = '', icon } = $props();
</script>

<div
  class="flex items-center gap-2 rounded-full border border-line bg-surface px-3 py-[7px] text-label font-medium text-ink-2"
>
  {#if dot}<span class="h-2 w-2 flex-shrink-0 rounded-full" style="background:{dot}"></span>{/if}
  {#if icon}{@const Icon = icon}<Icon size={14} class="text-ink-3" />{/if}
  {#if value !== ''}<b class="font-semibold text-ink">{value}</b>{/if}
  <span>{label}</span>
</div>
