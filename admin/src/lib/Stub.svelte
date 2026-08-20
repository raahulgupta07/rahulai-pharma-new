<script>
  let { title } = $props();
</script>

<header class="mb-4">
  <h1 class="page-title text-title leading-tight text-ink">{title}</h1>
</header>

<div
  class="rounded-card border border-line bg-surface px-6 py-10 text-center"
>
  <p class="text-body-sm font-semibold text-ink">Coming soon</p>
  <p class="mx-auto mt-1.5 max-w-sm text-meta text-ink-3">
    The {title.toLowerCase()} module is being built. Check back shortly.
  </p>
</div>
