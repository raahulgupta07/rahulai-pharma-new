<script>
  // A settings card: title + description on the left, toggle on the right.
  import Toggle from './Toggle.svelte';
  let { title = '', checked = $bindable(false), onchange, children } = $props();
</script>

<div
  class="elev mb-3.5 flex items-start gap-4 rounded-2xl border-[0.5px] border-line bg-surface p-[18px]"
>
  <div class="flex-1">
    <div class="mb-1.5 text-[14.5px] font-semibold text-ink">{title}</div>
    <div class="max-w-[560px] text-[13px] leading-relaxed text-ink-2">
      {@render children?.()}
    </div>
  </div>
  <Toggle bind:checked {onchange} label={title} />
</div>
