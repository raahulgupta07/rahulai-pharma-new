<script>
  // One block of the page: a heading, a one-line explanation of how to read it,
  // and the chart. The wrapper is what makes sections independent — a section
  // whose endpoint 404s or 500s renders its own panel and every other section
  // on the tab keeps working. One dead endpoint must never blank the page.
  import ErrorState from '$lib/ErrorState.svelte';

  let { title, hint = '', state = null, retry = null, what = 'this section', children } = $props();
</script>

<section class="my-7">
  <div class="mb-3 flex flex-wrap items-end gap-4">
    <div>
      <h3 class="page-title text-title font-semibold text-ink">{title}</h3>
      {#if hint}<p class="mt-0.5 text-meta text-ink-3">{hint}</p>{/if}
    </div>
  </div>

  {#if state && state.status === 'loading'}
    <div class="h-[170px] animate-pulse rounded-card border border-line bg-surface-2"></div>
  {:else if state && state.status === 'missing'}
    <!-- 404 is a deployment fact, not a fault: this console is newer than the
         backend it is talking to. Say which route is missing, plainly. -->
    <div class="rounded-card border border-dashed border-line-2 bg-surface-2 px-4 py-5 text-body-sm text-ink-2">
      <p class="font-medium text-ink">Not served by this backend</p>
      <p class="mt-1">
        <span class="font-mono text-meta">{state.path ?? what}</span> answered 404. The running build
        predates this endpoint, so this section has no data to show. Nothing here is being estimated
        in its place.
      </p>
    </div>
  {:else if state && state.status === 'error'}
    <ErrorState error={state.err} {retry} {what} />
  {:else}
    {@render children()}
  {/if}
</section>
