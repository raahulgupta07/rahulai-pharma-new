<script>
  import { onMount } from 'svelte';
  import { Play, Check, X, Loader } from '@lucide/svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import ErrorState from '$lib/ErrorState.svelte';
  import { getJSON } from '$lib/api.js';

  let running = $state(false);
  let error = $state(null);
  let result = $state(null);

  async function runEval() {
    running = true;
    error = null;
    result = null;
    try {
      // No client timeout — this can take 30-60s (live LLM calls).
      // getJSON, not a bare fetch: a 401 (expired token) and a stopped backend
      // are different failures and the panel must not report one as the other.
      result = await getJSON('/admin/eval/run', { method: 'POST' });
    } catch (e) {
      error = e;
    } finally {
      running = false;
    }
  }

  let total = $derived(result?.total ?? 0);
  let passed = $derived(result?.passed ?? 0);
  let cases = $derived(Array.isArray(result?.results) ? result.results : []);
  let allPass = $derived(total > 0 && passed === total);
  let percent = $derived(total > 0 ? Math.round((passed / total) * 100) : 0);
  let failed = $derived(total - passed);

  onMount(() => {});
</script>

<PageHeader
  level={2}
  title="Evaluation"
  subtitle="Runs the real screenshot questions (EN + Burmese) through the live agent and checks answers contain the correct data. Slow — makes LLM calls."
>
  {#snippet actions()}
    <button
      onclick={runEval}
      disabled={running}
      class="inline-flex items-center gap-2 rounded-panel bg-accent px-4 py-2.5 text-body-sm font-medium text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
    >
      {#if running}
        <Loader size={15} class="animate-spin" />
        Running…
      {:else}
        <Play size={15} />
        Run eval
      {/if}
    </button>
  {/snippet}
</PageHeader>

{#if error}
  <ErrorState {error} retry={runEval} what="the evaluation run" />
{:else if result}
  <!-- Summary -->
  <div class="mb-4 flex items-center gap-4 rounded-card border border-line bg-surface p-4.5">
    <div
      class="page-title text-display-lg leading-none tnum {allPass ? 'text-success' : 'text-danger'}"
    >
      {passed}/{total}
    </div>
    <div>
      <div class="text-body-sm font-medium text-ink">
        {allPass ? 'All passing' : `${failed} failed`}
      </div>
      <div class="text-body-sm text-ink-2">{percent}% passing</div>
    </div>
  </div>

  <!-- Per-case results -->
  <section>
    {#each cases as c (c.id)}
      <div class="mb-2 rounded-card border border-line p-3 {c.pass ? 'bg-surface' : 'bg-danger-soft'}">
        <div class="flex items-center gap-2.5">
          {#if c.pass}
            <Check size={16} class="shrink-0 text-success" />
          {:else}
            <X size={16} class="shrink-0 text-danger" />
          {/if}
          <span class="font-mono text-meta text-ink-3">{c.id}</span>
          <span class="text-body-sm font-medium text-ink">{c.question}</span>
        </div>
        {#if c.answer}
          <p class="answer-clamp ml-6 mt-1.5 text-meta leading-snug text-ink-2">
            {c.answer}
          </p>
        {/if}
      </div>
    {/each}
  </section>
{:else}
  <!-- Empty initial state -->
  <div
    class="rounded-card border border-line bg-surface px-5 py-6 text-body-sm text-ink-2"
  >
    Click Run eval to test the agent against the real question set.
  </div>
{/if}

<style>
  .answer-clamp {
    display: -webkit-box;
    -webkit-line-clamp: 3;
    line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
</style>
