<script>
  let { title } = $props();
</script>

<header class="mb-8">
  <h1 class="page-title text-[32px] leading-tight text-ink">{title}</h1>
</header>

<div
  class="rounded-lg border border-line bg-surface px-6 py-10 text-center"
>
  <p class="text-[15px] font-medium text-ink">Coming soon</p>
  <p class="mx-auto mt-1.5 max-w-sm text-[14px] text-ink-2">
    The {title.toLowerCase()} module is being built. Check back shortly.
  </p>
</div>
