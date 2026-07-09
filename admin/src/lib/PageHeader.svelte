<script>
  let { title = '', subtitle = '', actions, meta } = $props();
</script>

<header class="mb-6 flex flex-wrap items-start gap-4">
  <div class="min-w-0">
    <h1 class="page-title text-[30px] leading-tight text-ink">{title}</h1>
    {#if subtitle}
      <p class="mt-2 max-w-xl text-[14px] leading-relaxed text-ink-2">{subtitle}</p>
    {/if}
    {#if meta}<div class="mt-2 flex flex-wrap items-center gap-2">{@render meta()}</div>{/if}
  </div>
  {#if actions}
    <div class="ml-auto flex items-center gap-2">{@render actions()}</div>
  {/if}
</header>
